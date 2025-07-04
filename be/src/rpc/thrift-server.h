// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.

#pragma once

#include <mutex>
#include <boost/scoped_ptr.hpp>
#include <boost/shared_ptr.hpp>
#include <boost/unordered_map.hpp>
#include <boost/uuid/uuid_generators.hpp>
#include <thrift/TProcessor.h>
#include <thrift/server/TServer.h>
#include <thrift/transport/TBufferTransports.h>
#include <thrift/transport/TSSLSocket.h>

#include "common/status.h"
#include "gen-cpp/Frontend_types.h"
#include "kudu/security/security_flags.h"
#include "rpc/thrift-util.h"
#include "util/condition-variable.h"
#include "util/metrics-fwd.h"
#include "util/thread.h"

namespace apache {
namespace thrift {
namespace protocol {
class TProtocol;
}
namespace server {
class TAcceptQueueServer;
}
}
}

namespace impala {

class AuthProvider;

/// Utility class for all Thrift servers. Runs a TAcceptQueueServer server with, by
/// default, no enforced concurrent connection limit, that exposes the interface
/// described by a user-supplied TProcessor object.
///
/// Use a ThriftServerBuilder to construct a ThriftServer. ThriftServer's c'tors are
/// private.
/// TODO: shutdown is buggy (which only harms tests)
class ThriftServer {
 public:

   /// Override TBufferedTransport::close() to avoid calling flush() which is not safe
   /// in TSSLSocket if it is in error state. See IMPALA-13680 / THRIFT-5846 for details.
   class BufferedTransport :
      public apache::thrift::transport::TBufferedTransport {

    public:
      BufferedTransport(std::shared_ptr<apache::thrift::transport::TTransport>& transport,
          uint32_t sz, std::shared_ptr<apache::thrift::TConfiguration>&& config)
      : apache::thrift::transport::TBufferedTransport(transport, sz, config) {}

      // base implementation:
      // https://github.com/apache/thrift/blob/d078721e44fea7713832ae5d0f5d9ca67317f19e/lib/cpp/src/thrift/transport/TBufferTransports.h#L367
      virtual void close() override {
        transport_->close();
      }
  };

  /// Transport factory that wraps transports in a buffered transport with a customisable
  /// buffer-size and optionally in another transport from a provided factory. A larger
  /// buffer is usually more efficient, as it allows the underlying transports to perform
  /// fewer system calls.
  class BufferedTransportFactory :
      public apache::thrift::transport::TBufferedTransportFactory {
   public:
    static const int DEFAULT_BUFFER_SIZE_BYTES = 128 * 1024;

    BufferedTransportFactory(uint32_t buffer_size = DEFAULT_BUFFER_SIZE_BYTES,
        apache::thrift::transport::TTransportFactory* wrapped_factory =
            new apache::thrift::transport::TTransportFactory())
      : buffer_size_(buffer_size), wrapped_factory_(wrapped_factory) {}

    virtual std::shared_ptr<apache::thrift::transport::TTransport> getTransport(
        std::shared_ptr<apache::thrift::transport::TTransport> trans) {
      std::shared_ptr<apache::thrift::transport::TTransport> wrapped =
          wrapped_factory_->getTransport(trans);
      // Make sure the max message size got inherited properly.
      VerifyMaxMessageSizeInheritance(trans.get(), wrapped.get());
      std::shared_ptr<apache::thrift::transport::TTransport> buffered_wrapped =
          std::shared_ptr<apache::thrift::transport::TTransport>(
              new BufferedTransport(
                  wrapped, buffer_size_, wrapped->getConfiguration()));
      VerifyMaxMessageSizeInheritance(wrapped.get(), buffered_wrapped.get());
      return buffered_wrapped;
    }
   private:
    uint32_t buffer_size_;
    std::unique_ptr<apache::thrift::transport::TTransportFactory> wrapped_factory_;
  };

  /// Transport implementation used by the thrift server.
  enum TransportType {
    BINARY, // Thrift bytes over default transport.
    HTTP, // Thrift bytes over HTTP transport.
  };

  /// Username.
  typedef std::string Username;

  /// Per-connection information.
  struct ConnectionContext {
    TUniqueId connection_id;
    Username username;
    Username do_as_user;
    TNetworkAddress network_address;
    /// If using hs2-http protocol, this is the origin of the session
    /// as recorded in the X-Forwarded-For http message header.
    std::string http_origin;
    std::string server_name;
    /// Used to pass HTTP headers generated by the input transport to the output transport
    /// to be returned.
    std::vector<std::string> return_headers;
    std::string saml_response;
    std::string saml_relay_state;
    std::unique_ptr<TWrappedHttpRequest> request;
    std::unique_ptr<TWrappedHttpResponse> response;
    // Used in case of Kerberos authentication only to store the authenticated Kerberos
    // user principal
    std::string kerberos_user_principal;
    // Used in case of Kerberos authentication only to store the authenticated Kerberos
    // user principal's short name
    std::string kerberos_user_short;
  };

  /// Interface class for receiving connection creation / termination events.
  class ConnectionHandlerIf {
   public:
    /// Called when a connection is established (when a client connects).
    virtual void ConnectionStart(const ConnectionContext& connection_context) = 0;

    /// Called when a connection is terminated (when a client closes the connection).
    /// After this callback returns, the memory connection_context references is no longer
    /// valid and clients must not refer to it again.
    virtual void ConnectionEnd(const ConnectionContext& connection_context) = 0;

    /// Returns true if the connection is considered idle. A connection is considered
    /// idle if all the sessions associated with it have expired due to idle timeout.
    /// Called when a client has been inactive for --idle_client_poll_period_s seconds.
    virtual bool IsIdleConnection(const ConnectionContext& connection_context) = 0;

    virtual ~ConnectionHandlerIf() = default;
  };

  int port() const { return port_; }

  bool ssl_enabled() const { return ssl_enabled_; }

  /// Blocks until the server stops and exits its main thread.
  void Join();

  /// FOR TESTING ONLY; stop the server and block until the server is stopped
  void StopForTesting();

  /// Starts the main server thread. Once this call returns, clients
  /// may connect to this server and issue RPCs. May not be called more
  /// than once.
  Status Start();

  /// Sets the connection handler which receives events when connections are created or
  /// closed.
  void SetConnectionHandler(ConnectionHandlerIf* connection) {
    connection_handler_ = connection;
  }

  /// Returns true if the current thread has a connection context set on it.
  static bool HasThreadConnectionContext();

  /// Returns a unique identifier for the current connection. A connection is
  /// identified with the lifetime of a socket connection to this server.
  /// It is only safe to call this method during a Thrift processor RPC
  /// implementation. Otherwise, the result of calling this method is undefined.
  /// It is also only safe to reference the returned value during an RPC method.
  static const TUniqueId& GetThreadConnectionId();

  /// Returns a pointer to a struct that contains information about the current
  /// connection. This includes:
  ///   - A unique identifier for the connection.
  ///   - The username provided by the underlying transport for the current connection, or
  ///     an empty string if the transport did not provide a username. Currently, only the
  ///     TSasl transport provides this information.
  ///   - The client connection network address.
  /// It is only safe to call this method during a Thrift processor RPC
  /// implementation. Otherwise, the result of calling this method is undefined.
  /// It is also only safe to reference the returned value during an RPC method.
  static const ConnectionContext* GetThreadConnectionContext();

  typedef std::vector<std::shared_ptr<const ConnectionContext>> ConnectionContextList;

  /// Gets connection contexts for the thrift server.
  void GetConnectionContextList(ConnectionContextList* connection_contexts);

 private:
  friend class ThriftServerBuilder;
  friend class apache::thrift::server::TAcceptQueueServer;

  /// Helper class which monitors starting servers. Needs access to internal members, and
  /// is not used outside of this class.
  friend class ThriftServerEventProcessor;

  /// Helper class that starts a server in a separate thread, and handles
  /// the inter-thread communication to monitor whether it started
  /// correctly.
  class ThriftServerEventProcessor : public apache::thrift::server::TServerEventHandler {
   public:
    ThriftServerEventProcessor(ThriftServer* thrift_server)
      : thrift_server_(thrift_server),
        signal_fired_(false) { }

    /// Called by the Thrift server implementation when it has acquired its resources and
    /// is ready to serve, and signals to StartAndWaitForServer that start-up is finished.
    /// From TServerEventHandler.
    virtual void preServe();

    /// Called when a client connects; we create per-client state and call any
    /// ConnectionHandlerIf handler.
    virtual void* createContext(
        std::shared_ptr<apache::thrift::protocol::TProtocol> input,
        std::shared_ptr<apache::thrift::protocol::TProtocol> output);

    /// Called when a client starts an RPC; we set the thread-local connection context.
    virtual void processContext(void* context,
        std::shared_ptr<apache::thrift::transport::TTransport> output);

    /// Called when a client disconnects; we call any ConnectionHandlerIf handler.
    virtual void deleteContext(void* context,
        std::shared_ptr<apache::thrift::protocol::TProtocol> input,
        std::shared_ptr<apache::thrift::protocol::TProtocol> output);

    /// Returns true if a client's connection is idle. A client's connection is idle iff
    /// all the sessions associated with it have expired due to idle timeout. Called from
    /// TAcceptQueueServer::Task::run() after clients have been inactive for
    /// --idle_client_poll_period_s seconds.
    bool IsIdleContext(void* context);

    /// Waits for a timeout of TIMEOUT_MS for a server to signal that it has started
    /// correctly.
    Status StartAndWaitForServer();

   private:
    /// Lock used to ensure that there are no missed notifications between starting the
    /// supervision thread and calling signal_cond_.WaitUntil. Also used to ensure
    /// thread-safe access to members of thrift_server_
    std::mutex signal_lock_;

    /// Condition variable that is notified by the supervision thread once either
    /// a) all is well or b) an error occurred.
    ConditionVariable signal_cond_;

    /// The ThriftServer under management. This class is a friend of ThriftServer, and
    /// reaches in to change member variables at will.
    ThriftServer* thrift_server_;

    /// Guards against spurious condition variable wakeups
    bool signal_fired_;

    /// The time, in milliseconds, to wait for a server to come up
    static const int TIMEOUT_MS = 2500;

    /// Called in a separate thread
    void Supervise();
  };

  /// Creates, but does not start, a new server on the specified port
  /// that exports the supplied interface.
  ///  - name: human-readable name of this server. Should not contain spaces
  ///  - processor: Thrift processor to handle RPCs
  ///  - port: The port the server will listen for connections on
  ///  - auth_provider: Authentication scheme to use. If nullptr, use the global default
  ///    demon<->demon provider.
  ///  - metrics: if not nullptr, the server will register metrics on this object
  ///  - max_concurrent_connections: The maximum number of concurrent connections allowed.
  ///    If 0, there will be no enforced limit on the number of concurrent connections.
  ///  - queue_timeout_ms: amount of time in milliseconds an accepted client connection
  ///    will be held in the accepted queue, after which the request will be rejected if
  ///    a service thread can't be found. If 0, no timeout is enforced.
  ///  - idle_poll_period_ms: Amount of time, in milliseconds, of client's inactivity
  ///    before the service thread wakes up to check if the connection should be closed
  ///    due to inactivity. If 0, no polling happens.
  ///  - is_external_facing: Whether this ThriftServer interacts with untrusted/external
  ///    clients. This impacts the max message size used for Thrift, as untrusted
  ///    communication uses a more restrictive limit to protect against malicious
  ///    messages. When set to false, this uses a less restrictive max message size as
  ///    messages are constructed by other Impala cluster components. This defaults to
  ///    true to be safe by default.
  ThriftServer(const std::string& name,
      const std::shared_ptr<apache::thrift::TProcessor>& processor, int port,
      AuthProvider* auth_provider = nullptr, MetricGroup* metrics = nullptr,
      int max_concurrent_connections = 0, int64_t queue_timeout_ms = 0,
      int64_t idle_poll_period_ms = 0,
      TransportType server_transport = TransportType::BINARY,
      bool is_external_facing = true, std::string host = "");

  /// Enables secure access over SSL. Must be called before Start(). The first three
  /// arguments are the minimum SSL/TLS version, and paths to certificate and private key
  /// files in .PEM format, respectively. If either file does not exist, an error is
  /// returned. The fourth, optional, argument provides the command to run if a password
  /// is required to decrypt the private key. It is invoked once, and the resulting
  /// password is used only for password-protected .PEM files. The final argument is a
  /// string containing a list of cipher suites, separated by commas, to enable.
  Status EnableSsl(apache::thrift::transport::SSLProtocol version,
      const std::string& certificate, const std::string& private_key,
      const std::string& pem_password_cmd = "", const std::string& cipher_list = "",
      const std::string& tls_ciphersuites =
          kudu::security::SecurityDefaults::kDefaultTlsCipherSuites,
      bool disable_tls12 = false);

  /// Sets keepalive options for the client TCP connections. Keepalive is only enabled
  /// if probe_period_s > 0. These are the three standard keepalive settings for Linux:
  /// If a client connection is idle for probe_period_s seconds, it starts sending
  /// keepalives. If it doesn't hear back, it retries every retry_period_s seconds until
  /// it exceeds retry_count.
  void SetKeepAliveOptions(int32_t probe_period_s, int32_t retry_period_s,
      int32_t retry_count);

  /// Creates the server socket on which this server listens. May be SSL enabled. Returns
  /// OK unless there was a Thrift error.
  Status CreateSocket(std::shared_ptr<apache::thrift::transport::TServerSocket>* socket);

  /// True if the server has been successfully started, for internal use only
  bool started_;

  /// The port on which the server interface is exposed. Usually the port that was
  /// passed to the constructor, but if this was the wildcard port 0, then this is
  /// replaced with whatever port number the server is listening on.
  int port_;

  /// The host name to bind with.
  string host_;

  /// True if the server socket only accepts SSL connections
  bool ssl_enabled_;

  /// Path to certificate file in .PEM format
  std::string certificate_path_;

  /// Path to private key file in .PEM format
  std::string private_key_path_;

  /// Password string retrieved by running command in EnableSsl().
  std::string key_password_;

  /// List of ciphers that are ok for clients to use when connecting.
  std::string cipher_list_;

  /// List of TLSv1.3 cipher suites that are ok for clients to use when connecting.
  std::string tls_ciphersuites_;

  /// Whether to disable TLSv1.2. This is only used for testing TLSv1.3 ciphersuites.
  /// TODO: Remove this when it is possible to set ssl_minimum_version=TLSv1.3.
  bool disable_tls12_;

  /// The SSL/TLS protocol client versions that this server will allow to connect.
  apache::thrift::transport::SSLProtocol version_;

  /// Maximum number of concurrent connections (connections will block until fewer than
  /// max_concurrent_connections_ are concurrently active). If 0, there is no enforced
  /// limit.
  int max_concurrent_connections_;

  /// Amount of time in milliseconds an accepted client connection will be kept in the
  /// accept queue before it is timed out. If 0, there is no timeout.
  /// Used in TAcceptQueueServer.
  int64_t queue_timeout_ms_;

  /// Amount of time, in milliseconds, of client's inactivity before the service thread
  /// wakes up to check if the connection should be closed due to inactivity. If 0, no
  /// polling happens.
  int64_t idle_poll_period_ms_;

  /// User-specified identifier that shows up in logs
  const std::string name_;

  /// Identifier used to prefix all metric names that are produced by this server.
  const std::string metrics_name_;

  /// Thread that runs ThriftServerEventProcessor::Supervise() in a separate loop
  std::unique_ptr<Thread> server_thread_;

  /// Thrift housekeeping
  boost::scoped_ptr<apache::thrift::server::TServer> server_;
  std::shared_ptr<apache::thrift::TProcessor> processor_;

  /// If not nullptr, called when connection events happen. Not owned by us.
  ConnectionHandlerIf* connection_handler_;

  /// Protects connection_contexts_
  std::mutex connection_contexts_lock_;

  /// Map of active connection context to a shared_ptr containing that context; when an
  /// item is removed from the map, it is automatically freed.
  typedef boost::unordered_map<ConnectionContext*, std::shared_ptr<ConnectionContext>>
      ConnectionContextSet;
  ConnectionContextSet connection_contexts_;

  /// Metrics subsystem access
  MetricGroup* metrics_;

  /// True if metrics are enabled
  bool metrics_enabled_;

  /// Number of currently active connections
  IntGauge* num_current_connections_metric_;

  /// Total connections made over the lifetime of this server
  IntCounter* total_connections_metric_;

  /// Used to generate a unique connection id for every connection
  boost::uuids::random_generator uuid_generator_;

  /// Not owned by us, owned by the AuthManager
  AuthProvider* auth_provider_;

  /// Underlying transport type used by this thrift server.
  TransportType transport_type_;

  bool is_external_facing_;

  /// Keepalive options for client connections.
  int32_t keepalive_probe_period_s_ = 0;
  int32_t keepalive_retry_period_s_ = 0;
  int32_t keepalive_retry_count_ = 0;
};

/// Helper class to build new ThriftServer instances.
class ThriftServerBuilder {
 public:
  ThriftServerBuilder(const std::string& name,
      const std::shared_ptr<apache::thrift::TProcessor>& processor, int port)
    : name_(name), processor_(processor), port_(port) {}

  /// Sets the auth provider for this server. Default is the system global auth provider.
  ThriftServerBuilder& auth_provider(AuthProvider* provider) {
    auth_provider_ = provider;
    return *this;
  }

  /// Sets the metrics instance that this server should register metrics with. Default is
  /// nullptr.
  ThriftServerBuilder& metrics(MetricGroup* metrics) {
    metrics_ = metrics;
    return *this;
  }

  /// Sets the maximum concurrent thread count for this server. Default is 0, which means
  /// there is no enforced limit.
  ThriftServerBuilder& max_concurrent_connections(int max_concurrent_connections) {
    max_concurrent_connections_ = max_concurrent_connections;
    return *this;
  }

  ThriftServerBuilder& queue_timeout_ms(int64_t timeout_ms) {
    queue_timeout_ms_ = timeout_ms;
    return *this;
  }

  ThriftServerBuilder& idle_poll_period_ms(int64_t timeout_ms) {
    idle_poll_period_ms_ = timeout_ms;
    return *this;
  }

  /// Enables SSL for this server.
  ThriftServerBuilder& ssl(
      const std::string& certificate, const std::string& private_key) {
    enable_ssl_ = true;
    certificate_ = certificate;
    private_key_ = private_key;
    return *this;
  }

  /// Sets the SSL/TLS client version(s) that this server will allow to connect.
  ThriftServerBuilder& ssl_version(apache::thrift::transport::SSLProtocol version) {
    version_ = version;
    return *this;
  }

  /// Sets the command used to compute the password for the SSL private key. Default is
  /// empty, i.e. no password needed.
  ThriftServerBuilder& pem_password_cmd(const std::string& pem_password_cmd) {
    pem_password_cmd_ = pem_password_cmd;
    return *this;
  }

  /// Sets the list of acceptable cipher suites for this server. Default is to use all
  /// available system cipher suites.
  ThriftServerBuilder& cipher_list(const std::string& cipher_list) {
    cipher_list_ = cipher_list;
    return *this;
  }

  /// Sets the list of TLS 1.3 ciphersuites for this server. Default is to
  /// use all available TLS 1.3 ciphersuites.
  ThriftServerBuilder& tls_ciphersuites(const std::string& tls_ciphersuites) {
    tls_ciphersuites_ = tls_ciphersuites;
    return *this;
  }

  /// Sets whether to disable TLS 1.2. This is used for testing TLS 1.3.
  /// TODO: Remove this when ssl_minimum_version=tlsv1.3 is supported.
  ThriftServerBuilder& disable_tls12(bool disable) {
    disable_tls12_ = disable;
    return *this;
  }

  /// Sets the underlying transport type for the thrift server.
  ThriftServerBuilder& transport_type(ThriftServer::TransportType transport_type) {
    server_transport_type_ = transport_type;
    return *this;
  }

  /// Sets whether the Thrift server will interact with external non-Impala clients.
  /// If set to true, this uses ThriftExternalRpcMaxMessageSize(). If set to false,
  /// this uses ThriftInternalRpcMaxMessageSize().
  ThriftServerBuilder& is_external_facing(bool is_external_facing) {
    is_external_facing_ = is_external_facing;
    return *this;
  }

  /// Sets keepalive options for the client TCP connections. Keepalive is only enabled
  /// if probe_period_s > 0. These are the three standard keepalive settings for Linux:
  /// If a client connection is idle for probe_period_s seconds, it starts sending
  /// keepalives. If it doesn't hear back, it retries every retry_period_s seconds until
  /// it exceeds retry_count.
  ThriftServerBuilder& keepalive(int32_t probe_period_s,
      int32_t retry_period_s, int32_t retry_count) {
    keepalive_probe_period_s_ = probe_period_s;
    keepalive_retry_period_s_ = retry_period_s;
    keepalive_retry_count_ = retry_count;
    return *this;
  }

  ThriftServerBuilder& host(const string& host) {
    host_ = host;
    return *this;
  }

  /// Constructs a new ThriftServer and puts it in 'server', if construction was
  /// successful, returns an error otherwise. In the error case, 'server' will not have
  /// been set and will not need to be freed, otherwise the caller assumes ownership of
  /// '*server'.
  Status Build(ThriftServer** server) {
    std::unique_ptr<ThriftServer> ptr(
        new ThriftServer(name_, processor_, port_, auth_provider_, metrics_,
            max_concurrent_connections_, queue_timeout_ms_, idle_poll_period_ms_,
            server_transport_type_, is_external_facing_, host_));
    if (enable_ssl_) {
      RETURN_IF_ERROR(ptr->EnableSsl(
          version_, certificate_, private_key_, pem_password_cmd_, cipher_list_,
          tls_ciphersuites_, disable_tls12_));
    }
    ptr->SetKeepAliveOptions(keepalive_probe_period_s_, keepalive_retry_period_s_,
        keepalive_retry_count_);
    (*server) = ptr.release();
    return Status::OK();
  }

 private:
  int64_t queue_timeout_ms_ = 0;
  int64_t idle_poll_period_ms_ = 0;
  int max_concurrent_connections_ = 0;
  std::string name_;
  std::shared_ptr<apache::thrift::TProcessor> processor_;
  std::string host_;
  int port_ = 0;
  ThriftServer::TransportType server_transport_type_ =
      ThriftServer::TransportType::BINARY;

  AuthProvider* auth_provider_ = nullptr;
  MetricGroup* metrics_ = nullptr;

  bool enable_ssl_ = false;
  apache::thrift::transport::SSLProtocol version_ =
      apache::thrift::transport::SSLProtocol::TLSv1_0;
  std::string certificate_;
  std::string private_key_;
  std::string pem_password_cmd_;
  std::string cipher_list_;
  std::string tls_ciphersuites_ =
      kudu::security::SecurityDefaults::kDefaultTlsCipherSuites;
  bool disable_tls12_ = false;
  bool is_external_facing_ = true;
  int32_t keepalive_probe_period_s_ = 0;
  int32_t keepalive_retry_period_s_ = 0;
  int32_t keepalive_retry_count_ = 0;
};

/// Contains a map from string for --ssl_minimum_version to Thrift's SSLProtocol.
struct SSLProtoVersions {
  static std::map<std::string, apache::thrift::transport::SSLProtocol> PROTO_MAP;

  /// Given a string, find a corresponding SSLProtocol from PROTO_MAP. Returns an error if
  /// one cannot be found. Matching is case-insensitive.
  static Status StringToProtocol(
      const std::string& in, apache::thrift::transport::SSLProtocol* protocol);

  /// Returns true if 'protocol' is supported by the version of OpenSSL this binary is
  /// linked to.
  static bool IsSupported(const apache::thrift::transport::SSLProtocol& protocol);
};

}
