#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import absolute_import, print_function, unicode_literals
import base64
from datetime import datetime
import operator
import re
import socket
import ssl
import sys
import time
import traceback
import uuid

from bitarray import bitarray
import sasl
from thrift.protocol import TBinaryProtocol
from thrift.Thrift import TApplicationException, TException
from thrift.transport.TSocket import TSocket
from thrift.transport.TTransport import TBufferedTransport, TTransportException
from thrift_sasl import TSaslClientTransport

from impala_shell.compatibility import _xrange as xrange
from impala_shell.exec_summary import build_exec_summary_table
from impala_shell.ImpalaHttpClient import ImpalaHttpClient
from impala_shell.kerberos_util import get_kerb_host_from_kerberos_host_fqdn
from impala_shell.shell_exceptions import (
    DisconnectedException,
    HttpError,
    MissingThriftMethodException,
    QueryCancelledByShellException,
    QueryStateException,
    RPCException,
)
from impala_shell.thrift_printer import ThriftPrettyPrinter
from impala_shell.value_converter import HS2ValueConverter
from impala_thrift_gen.beeswax import BeeswaxService
from impala_thrift_gen.beeswax.BeeswaxService import QueryState
from impala_thrift_gen.ErrorCodes.ttypes import TErrorCode
from impala_thrift_gen.ImpalaService import ImpalaHiveServer2Service, ImpalaService
from impala_thrift_gen.ImpalaService.ImpalaHiveServer2Service import (
    TCloseImpalaOperationReq,
    TGetExecSummaryReq,
    TGetRuntimeProfileReq,
    TPingImpalaHS2ServiceReq,
)
from impala_thrift_gen.Status.ttypes import TStatus
from impala_thrift_gen.TCLIService.TCLIService import (
    TCancelOperationReq,
    TCloseOperationReq,
    TCloseSessionReq,
    TExecuteStatementReq,
    TFetchOrientation,
    TFetchResultsReq,
    TGetLogReq,
    TGetOperationStatusReq,
    TGetResultSetMetadataReq,
    TOpenSessionReq,
    TOperationState,
    TProtocolVersion,
    TStatusCode,
    TTypeId,
)

# Getters to extract HS2's representation of values to the display version.
# An entry must be added to this map for each supported type. HS2's TColumn has many
# different typed field, each of which has a 'values' and a 'nulls' field. These getters
# extract the appropriate member from TColumn for the given TTypeId.
HS2_VALUE_GETTERS = {
    TTypeId.BOOLEAN_TYPE: operator.attrgetter('boolVal'),
    TTypeId.TINYINT_TYPE: operator.attrgetter('byteVal'),
    TTypeId.SMALLINT_TYPE: operator.attrgetter('i16Val'),
    TTypeId.INT_TYPE: operator.attrgetter('i32Val'),
    TTypeId.BIGINT_TYPE: operator.attrgetter('i64Val'),
    TTypeId.TIMESTAMP_TYPE: operator.attrgetter('stringVal'),
    TTypeId.FLOAT_TYPE: operator.attrgetter('doubleVal'),
    TTypeId.DOUBLE_TYPE: operator.attrgetter('doubleVal'),
    TTypeId.STRING_TYPE: operator.attrgetter('stringVal'),
    TTypeId.DECIMAL_TYPE: operator.attrgetter('stringVal'),
    TTypeId.BINARY_TYPE: operator.attrgetter('binaryVal'),
    TTypeId.VARCHAR_TYPE: operator.attrgetter('stringVal'),
    TTypeId.CHAR_TYPE: operator.attrgetter('stringVal'),
    TTypeId.MAP_TYPE: operator.attrgetter('stringVal'),
    TTypeId.ARRAY_TYPE: operator.attrgetter('stringVal'),
    TTypeId.STRUCT_TYPE: operator.attrgetter('stringVal'),
    TTypeId.UNION_TYPE: operator.attrgetter('stringVal'),
    TTypeId.NULL_TYPE: operator.attrgetter('stringVal'),
    TTypeId.DATE_TYPE: operator.attrgetter('stringVal')
}


# Helper to decode utf8 encoded str to unicode type in Python 2. NOOP in Python 3.
def utf8_decode_if_needed(val):
  if sys.version_info.major < 3 and isinstance(val, str):
    val = val.decode('utf-8', errors='replace')
  return val


# Helper to decode unicode to utf8 encoded str in Python 2. NOOP in Python 3.
def utf8_encode_if_needed(val):
  if sys.version_info.major < 3 and isinstance(val, unicode):  # noqa: F821
    val = val.encode('utf-8', errors='replace')
  return val


# Regular expression that matches the progress line added to HS2 logs by
# the Impala server.
HS2_LOG_PROGRESS_REGEX = re.compile(r"Query.*Complete \([0-9]* out of [0-9]*\)\n")

# Exception types to differentiate between the different RPCExceptions.
# RPCException raised when TApplicationException is caught.
RPC_EXCEPTION_TAPPLICATION = "TAPPLICATION_EXCEPTION"
# RPCException raised when impala server sends a TStatusCode.ERROR_STATUS status code.
RPC_EXCEPTION_SERVER = "SERVER_ERROR"


class QueryOptionLevels:
  """These are the levels used when displaying query options.
  The values correspond to the ones in TQueryOptionLevel"""
  REGULAR = 0
  ADVANCED = 1
  DEVELOPMENT = 2
  DEPRECATED = 3
  REMOVED = 4

  # Map from level name to the level's numeric value.
  NAME_TO_VALUES = {'REGULAR': 0, 'ADVANCED': 1, 'DEVELOPMENT': 2,
                    'DEPRECATED': 3, 'REMOVED': 4}

  @classmethod
  def from_string(cls, string):
    """Return the integral value based on the string. Defaults to DEVELOPMENT."""
    return cls.NAME_TO_VALUES.get(string.upper(), cls.DEVELOPMENT)


class ImpalaClient(object):
  """Base class for shared functionality between HS2 and Beeswax. Includes stub methods
  for methods that are expected to be implemented in the subclasses.
  TODO: when beeswax support is removed, merge this with ImpalaHS2Client."""
  def __init__(self, impalad, fetch_size, kerberos_host_fqdn, use_kerberos=False,
               kerberos_service_name="impala", use_ssl=False, ca_cert=None, user=None,
               ldap_password=None, use_ldap=False, client_connect_timeout_ms=60000,
               verbose=True, use_http_base_transport=False, http_path=None,
               http_cookie_names=None, http_socket_timeout_s=None, value_converter=None,
               connect_max_tries=4, rpc_stdout=False, rpc_file=None, http_tracing=True,
               jwt=None, oauth=None, hs2_x_forward=None):
    self.connected = False
    self.impalad_host = impalad[0]
    self.impalad_port = int(impalad[1])
    self.kerberos_host_fqdn = kerberos_host_fqdn
    self.imp_service = None
    self.transport = None
    self.use_kerberos = use_kerberos
    self.kerberos_service_name = kerberos_service_name
    self.use_ssl = use_ssl
    self.ca_cert = ca_cert
    self.user, self.ldap_password = user, ldap_password
    self.use_ldap = use_ldap
    self.client_connect_timeout_ms = int(client_connect_timeout_ms)
    self.http_socket_timeout_s = http_socket_timeout_s
    self.connect_max_tries = connect_max_tries
    self.default_query_options = {}
    self.query_option_levels = {}
    self.fetch_size = fetch_size
    self.use_http_base_transport = use_http_base_transport
    self.http_path = http_path
    self.http_cookie_names = http_cookie_names
    self.http_tracing = http_tracing
    self.jwt = jwt
    self.oauth = oauth
    # This is set from ImpalaShell's signal handler when a query is cancelled
    # from command line via CTRL+C. It is used to suppress error messages of
    # query cancellation.
    self.is_query_cancelled = False
    self.verbose = verbose
    # This is set in connect(). It's used in constructing the retried query link after
    # we parse the retried query id.
    self.webserver_address = None
    self.value_converter = value_converter
    self.rpc_stdout = rpc_stdout
    self.rpc_file = rpc_file
    # In h2s-http clients only, the value of the X-Forwarded-For http header.
    self.hs2_x_forward = hs2_x_forward

  def connect(self):
    """Creates a connection to an Impalad instance. Returns a tuple with the impala
    version string and the webserver address, otherwise raises an exception. If the client
    was already connected, closes the previous connection."""
    self.close_connection()

    if self.use_http_base_transport:
        self.transport = self._get_http_transport(self.client_connect_timeout_ms)
    else:
        self.transport = self._get_transport(self.client_connect_timeout_ms)
    assert self.transport and self.transport.isOpen()

    if self.verbose:
      msg = 'Opened TCP connection to %s:%s' % (self.impalad_host, self.impalad_port)
      print(msg, file=sys.stderr)
    protocol = TBinaryProtocol.TBinaryProtocolAccelerated(self.transport)
    self.imp_service = self._get_thrift_client(protocol)
    self.connected = True
    try:
      self._open_session()
      return self._ping_impala_service()
    except Exception:
      # Ensure we are in a disconnected state if we failed above.
      self.close_connection()
      raise

  def _get_thrift_client(self, protocol):
    """Instantiate a thrift client with the provided protocol."""
    raise NotImplementedError()

  def _open_session(self):
    """Does any work to open a session for a new connection.
    Also sets up self.default_query_options and self.query_option_levels
    to include metadata about the options supported by the server."""
    raise NotImplementedError()

  def is_connected(self):
    """Returns True if the current Impala connection is alive and False otherwise."""
    if not self.connected:
      return False
    try:
      self._ping_impala_service()
      return True
      # Catch exceptions that are associated with communication errors.
    except TException:
      self.close_connection()
      return False
    except RPCException:
      self.close_connection()
      return False
    except DisconnectedException:
      self.close_connection()
      return False

  def close_connection(self):
    """Close any open sessions and close the connection if still open."""
    raise NotImplementedError()

  def _close_transport(self):
    """Closes transport if not closed and set self.connected to False. This is the last
    step of close_connection()."""
    if self.transport and self.transport.isOpen():
      self.transport.close()
    self.connected = False

  def _ping_impala_service(self):
    """Pings the Impala service to ensure it can receive RPCs. Returns a tuple with
    the impala version string and the webserver address. Raise TException, RPCException,
    DisconnectedException or MissingThriftMethodException if it cannot successfully
    communicate with the Impala daemon."""
    raise NotImplementedError()

  def execute_query(self, query_str, set_query_options):
    """Execute the query 'query_str' asynchronously on the server with options dictionary
    'set_query_options' and return a query handle that can be used for subsequent
    ImpalaClient method calls for the query. The handle returned is
    implementation-dependent but is guaranteed to have an 'is_closed' member
    that reflects whether the query was closed with close_query() or close_dml()"""
    raise NotImplementedError()

  def get_query_id_str(self, last_query_handle):
    """Return the standard string representation of an Impala query ID, e.g.
    'd74d8ce632c9d4d0:75c5a51100000000'"""
    raise NotImplementedError()

  def get_query_link(self, query_id):
    """Return the URL link to the debug page of the query"""
    return "%s/query_plan?query_id=%s" % (self.webserver_address, query_id)

  def wait_to_finish(self, last_query_handle, periodic_callback=None):
    """Wait until the results can be fetched for 'last_query_handle' or until the
    query encounters an error or is cancelled. Raises an exception if the query
    encounters an error or is cancelled or if we lose connection to the impalad.
    If 'periodic_callback' is provided, it is called periodically with no arguments."""
    loop_start = time.time()
    while True:
      start_rpc_time = time.time()
      query_state = self.get_query_state(last_query_handle)
      rpc_time = time.time() - start_rpc_time
      if query_state == self.FINISHED_STATE:
        break
      elif query_state in (self.ERROR_STATE, self.CANCELED_STATE):
        if self.connected:
          # TODO: does this do the right thing for a cancelled query?
          raise QueryStateException(self.get_error_log(last_query_handle))
        else:
          raise DisconnectedException("Not connected to impalad.")

      if periodic_callback is not None: periodic_callback()
      sleep_time = self._get_sleep_interval(loop_start)
      if rpc_time < sleep_time:
        time.sleep(sleep_time - rpc_time)

  def get_query_state(self, last_query_handle):
    """Return the query state string for 'last_query_handle'. Returns self.ERROR_STATE
    if there is an error communicating with the server or the client is disconnected.
    """
    raise NotImplementedError()

  def get_column_names(self, last_query_handle):
    """Get a list of column names for the query. The query must have a result set."""
    raise NotImplementedError()

  def expect_result_metadata(self, query_str, query_handle):
    """Given a query string and handle, return True if impalad expects result metadata."""
    raise NotImplementedError()

  def fetch(self, query_handle):
    """Returns an iterable of batches of result rows. Each batch is an iterable of rows.
    Each row is an iterable of strings in the format in which they should be displayed
    Tries to ensure that the batches have a granularity of self.fetch_size but
    does not guarantee it.
    """
    """Returns an iterable of batches of result rows up to self.fetch_size. Does
    not need to consolidate those batches into larger batches."""
    raise NotImplementedError()

  # TODO: when we remove Beeswax, we could merge close_dml() and close_query()
  # because the CloseImpalaOperation() response contains enough information to
  # differentiate between DML and non-DML.
  def close_dml(self, last_query_handle):
    """Fetches the results of a DML query. Returns a tuple containing the
       number of rows modified, the number of rows deleted, and the number of row errors,
       in that order. If the DML operation doesn't return 'rows_deleted' or
       'num_row_errors', then the respective element in the tuple is None.
       Returns None if the query was not closed successfully. Not idempotent."""
    raise NotImplementedError()

  def close_query(self, last_query_handle):
    """Close the query handle. Idempotent - after the first attempt, closing the same
    query handle is a no-op. Returns True if the query was closed
    successfully or False otherwise."""
    raise NotImplementedError()

  def cancel_query(self, last_query_handle):
    """Cancel a query on a keyboard interrupt from the shell. Return True if the
    query was previously cancelled or if the cancel operation succeeded. Return
    False otherwise."""
    raise NotImplementedError()

  def get_runtime_profile(self, last_query_handle):
    """Get the runtime profile string from the server. Returns None if
    an error was encountered. If the query was retried, returns the profile of the failed
    attempt as well; the tuple (profile, failed_profile) is returned where 'profile' is
    the profile of the most recent query attempt and 'failed_profile' is the profile of
    the original query attempt that failed. Currently, only the HS2 protocol supports
    returning the failed profile."""
    raise NotImplementedError()

  def get_summary(self, last_query_handle):
    """Get the thrift TExecSummary from the server. Returns None if
    an error was encountered. If the query was retried, returns TExecSummary of the failed
    attempt as well; the tuple (summary, failed_summary) is returned where 'summary' is
    the TExecSummary of the most recent query attempt and 'failed_summary' is the
    TExecSummary of the original query attempt that failed. Currently, only the HS2
    protocol supports returning the failed summary"""
    raise NotImplementedError()

  def _get_warn_or_error_log(self, last_query_handle, warn):
    """Returns all messages from the error log prepended with 'WARNINGS:' or 'ERROR:' for
    last_query_handle, depending on whether warn is True or False. Note that the error
    log may contain messages that are not errors (e.g. warnings)."""
    raise NotImplementedError()

  def get_warning_log(self, last_query_handle):
    """Returns all messages from the error log prepended with 'WARNINGS:' for
    last_query_handle. Note that the error log may contain messages that are not errors
    (e.g. warnings)."""
    return self._get_warn_or_error_log(last_query_handle, True)

  def get_error_log(self, last_query_handle):
    """Returns all messages from the error log prepended with 'ERROR:' for
    last_query_handle. Note that the error log may contain messages that are not errors
    (e.g. warnings)."""
    return self._get_warn_or_error_log(last_query_handle, False)

  def _append_retried_query_link(self, get_log_result):
    """Append the retried query link if the original query has been retried"""
    if self.webserver_address:
      query_id_search = re.search("Query has been retried using query id: (.*)\n",
                                  get_log_result)
      if query_id_search and len(query_id_search.groups()) >= 1:
        retried_query_id = query_id_search.group(1)
        get_log_result += "Retried query link: %s" % \
                          self.get_query_link(retried_query_id)
    return get_log_result

  def _get_http_transport(self, connect_timeout_ms):
    """Creates a transport with HTTP as the base."""
    # Older python versions do not support SSLContext needed by ImpalaHttpClient. More
    # context in IMPALA-8864. CentOs 6 ships such an incompatible python version
    # out of the box.
    if not hasattr(ssl, "create_default_context"):
      print("Python version too old. SSLContext not supported.", file=sys.stderr)
      raise NotImplementedError()
    # Current implementation of ImpalaHttpClient does a close() and open() of the
    # underlying http connection on every flush() (THRIFT-4600). Due to this, setting a
    # connect timeout does not achieve the desirable result as the subsequent open() could
    # block similary in case of problematic remote end points.
    # TODO: Investigate connection reuse in ImpalaHttpClient and revisit this.
    if connect_timeout_ms > 0 and self.verbose:
      print("Warning: --connect_timeout_ms is currently ignored with HTTP transport.",
            file=sys.stderr)

    # Notes on http socket timeout:
    # https://docs.python.org/3/library/socket.html#socket-timeouts
    # Having a default timeout of 'None' (blocking mode) could result in hang like
    # symptoms in case of a problematic remote endpoint. It's better to have a finite
    # timeout so that in case of any connection errors, the client retries have a better
    # chance of succeeding.
    host_and_port = self._to_host_port(self.impalad_host, self.impalad_port)
    assert self.http_path
    # ImpalaHttpClient relies on the URI scheme (http vs https) to open an appropriate
    # connection to the server.
    if self.use_ssl:
      ssl_ctx = ssl.create_default_context(cafile=self.ca_cert)
      if self.ca_cert:
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED
      else:
        ssl_ctx.check_hostname = False  # Mandated by the SSL lib for CERT_NONE mode.
        ssl_ctx.verify_mode = ssl.CERT_NONE
      url = "https://{0}/{1}".format(host_and_port, self.http_path)
      transport = ImpalaHttpClient(url, ssl_context=ssl_ctx,
                                   http_cookie_names=self.http_cookie_names,
                                   socket_timeout_s=self.http_socket_timeout_s,
                                   verbose=self.verbose)
    else:
      url = "http://{0}/{1}".format(host_and_port, self.http_path)
      transport = ImpalaHttpClient(url, http_cookie_names=self.http_cookie_names,
                                   socket_timeout_s=self.http_socket_timeout_s,
                                   verbose=self.verbose)

    if self.use_ldap:
      # Set the BASIC authorization
      user_passwd = "{0}:{1}".format(self.user, self.ldap_password)
      # Produce RFC 2617-compliant basic credentials:
      #  - RFC 2045 encoding of username:password without limitations to 76 chars
      #    per line (and without trailing newline)
      #  - No translation of characters (+,/) for URL-safety
      auth = base64.b64encode(user_passwd.encode()).decode()
      transport.setLdapAuth(auth)
    elif self.jwt is not None:
      transport.setJwtAuth(self.jwt)
    elif self.oauth is not None:
      transport.setOAuthAuth(self.oauth)
    elif self.use_kerberos or self.kerberos_host_fqdn:
      # Set the Kerberos service
      if self.kerberos_host_fqdn is not None:
        kerb_host = get_kerb_host_from_kerberos_host_fqdn(self.kerberos_host_fqdn)
      else:
        kerb_host = self.impalad_host
      kerb_service = "{0}@{1}".format(self.kerberos_service_name, kerb_host)
      transport.setKerberosAuth(kerb_service)
    else:
      transport.setNoneAuth()

    transport.addCustomHeaderFunc(self.get_custom_http_headers)

    # Without buffering Thrift would call socket.recv() each time it deserializes
    # something (e.g. a member in a struct).
    transport = TBufferedTransport(transport)
    transport.open()
    return transport

  def _get_transport(self, connect_timeout_ms):
    """Create a Transport.

       A non-kerberized impalad just needs a simple buffered transport. For
       the kerberized version, a sasl transport is created.

       If SSL is enabled, a TSSLSocket underlies the transport stack; otherwise a TSocket
       is used.
       This function returns the socket and the transport object.
    """
    if self.use_ssl:
      # TSSLSocket needs the ssl module, which may not be standard on all Operating
      # Systems. Only attempt to import TSSLSocket if the user wants an SSL connection.
      from impala_shell.TSSLSocketWithWildcardSAN import TSSLSocketWithWildcardSAN

    # The kerberos_host_fqdn option exposes the SASL client's hostname attribute to
    # the user. impala-shell checks to ensure this host matches the host in the kerberos
    # principal. So when a load balancer is configured to be used, its hostname is
    # expected by impala-shell. Setting this option to the load balancer hostname allows
    # impala-shell to connect directly to an impalad.
    if self.kerberos_host_fqdn is not None:
      sasl_host = get_kerb_host_from_kerberos_host_fqdn(self.kerberos_host_fqdn)
    else:
      sasl_host = self.impalad_host

    # Always use the hostname and port passed in to -i / --impalad as the host for the
    # purpose of creating the actual socket.
    sock_host = self.impalad_host
    sock_port = self.impalad_port
    if self.use_ssl:
      if self.ca_cert is None:
        # No CA cert means don't try to verify the certificate
        sock = TSSLSocketWithWildcardSAN(sock_host, sock_port, validate=False)
      else:
        sock = TSSLSocketWithWildcardSAN(
            sock_host, sock_port, validate=True, ca_certs=self.ca_cert)
    else:
      sock = TSocket(sock_host, sock_port)

    if connect_timeout_ms > 0: sock.setTimeout(connect_timeout_ms)

    # Helper to initialize a sasl client
    def sasl_factory():
      sasl_client = sasl.Client()
      sasl_client.setAttr("host", sasl_host)
      if self.use_ldap:
        sasl_client.setAttr("username", self.user)
        sasl_client.setAttr("password", self.ldap_password)
      else:
        sasl_client.setAttr("service", self.kerberos_service_name)
      sasl_client.init()
      return sasl_client

    transport = None
    if not (self.use_ldap or self.use_kerberos):
      transport = TBufferedTransport(sock)
    # GSSASPI is  the underlying mechanism used by kerberos to authenticate.
    elif self.use_kerberos:
      transport = TSaslClientTransport(sasl_factory, "GSSAPI", sock)
    else:
      transport = TSaslClientTransport(sasl_factory, "PLAIN", sock)
    # Open the transport and reset the timeout so that it does not apply to the
    # subsequent RPCs on the same socket.
    transport.open()
    sock.setTimeout(None)
    return transport

  def build_summary_table(self, summary, output):
    build_exec_summary_table(summary, 0, 0, False, output, is_prettyprint=True,
                             separate_prefix_column=False)

  def _get_sleep_interval(self, start_time):
    """Returns a step function of time to sleep in seconds before polling
    again. Maximum sleep is 1s, minimum is 0.1s"""
    elapsed = time.time() - start_time
    if elapsed < 10.0:
      return 0.1
    elif elapsed < 60.0:
      return 0.5
    return 1.0

  def _check_connected(self):
    """Raise DiconnectedException if the client is not connected."""
    if not self.connected:
      raise DisconnectedException("Not connected (use CONNECT to establish a connection)")

  def get_custom_http_headers(self):
    # When the transport is http, subclasses can override this function
    # to add arbitrary http headers.
    return None

  def _process_dml_result(self, dml_result):
    num_rows = sum([int(k) for k in dml_result.rows_modified.values()])
    num_deleted_rows = None
    if dml_result.rows_deleted:
      num_deleted_rows = sum([int(k) for k in dml_result.rows_deleted.values()])
    return (num_rows, num_deleted_rows, dml_result.num_row_errors)

  @staticmethod
  def _to_host_port(host, port):
    # Wrap ipv6 addresses in brackets.
    is_ipv6_address = ":" in host
    fmt = "[{0}]:{1}" if is_ipv6_address else "{0}:{1}"
    return fmt.format(host, port)


class ImpalaHS2Client(ImpalaClient):
  """Impala client. Uses the HS2 protocol plus Impala-specific extensions."""
  def __init__(self, *args, **kwargs):
    super(ImpalaHS2Client, self).__init__(*args, **kwargs)
    self.FINISHED_STATE = TOperationState._NAMES_TO_VALUES["FINISHED_STATE"]
    self.ERROR_STATE = TOperationState._NAMES_TO_VALUES["ERROR_STATE"]
    self.CANCELED_STATE = TOperationState._NAMES_TO_VALUES["CANCELED_STATE"]
    self._clear_current_query_handle()

    # If connected, this is the handle returned by the OpenSession RPC that needs
    # to be passed into most HS2 RPCs.
    self.session_handle = None
    # Enable retries only for hs2-http protocol.
    if self.use_http_base_transport:
      # Maximum number of tries for idempotent rpcs.
      self.max_tries = self.connect_max_tries
    else:
      self.max_tries = 1
    # Minimum sleep interval between retry attempts.
    self.min_sleep_interval = 1

    # In case of direct instantiation of the client where the converter is
    # not set, there should be a default value converter assigned
    if self.value_converter is None:
      self.value_converter = HS2ValueConverter()

    if self.rpc_stdout or self.rpc_stdout is not None:
      self.thrift_printer = ThriftPrettyPrinter()

    self._base_request_id = str(uuid.uuid1())
    self._request_num = 0

  def _get_thrift_client(self, protocol):
    return ImpalaHiveServer2Service.Client(protocol)

  def _get_sleep_interval_for_retries(self, num_tries):
    """Returns the sleep interval in seconds for the 'num_tries' retry attempt."""
    assert num_tries > 0 and num_tries < self.max_tries
    return self.min_sleep_interval * (num_tries - 1)

  def _open_session(self):
    def OpenSession(req):
      return self.imp_service.OpenSession(req)
    # OpenSession rpcs are idempotent and so ok to retry. If the client gets disconnected
    # and the server successfully opened a session, the client will retry and rely on
    # server to clean up the session.
    req = TOpenSessionReq(TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6,
                          username=self.user)
    resp = self._do_hs2_rpc(OpenSession, req, retry_on_error=True)
    self._check_hs2_rpc_status(resp.status)
    assert (resp.serverProtocolVersion
            == TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6), resp.serverProtocolVersion
    # TODO: ensure it's closed if needed
    self.session_handle = resp.sessionHandle

    self._populate_query_options()

  def get_custom_http_headers(self):
    headers = {}

    if self.http_tracing:
      session_id = self.get_session_id()
      if session_id is not None:
        headers["X-Impala-Session-Id"] = session_id

      current_query_id = self.get_query_id_str(self._current_query_handle)
      if current_query_id is not None:
        headers["X-Impala-Query-Id"] = current_query_id

      assert getattr(self, "_current_request_id", None) is not None, \
        "request id was not set"
      headers["X-Request-Id"] = self._current_request_id
    if self.hs2_x_forward:
      headers["X-Forwarded-For"] = self.hs2_x_forward

    return headers

  def close_connection(self):
    if self.session_handle is not None:
      # Attempt to close session explicitly. Do not fail if there is an error
      # doing so. We still need to close the transport and we can rely on the
      # server to clean up the session.
      try:
        def CloseSession(req):
          return self.imp_service.CloseSession(req)
        # CloseSession rpcs don't need retries since we catch all exceptions and close
        # transport.
        req = TCloseSessionReq(self.session_handle)
        resp = self._do_hs2_rpc(CloseSession, req)
        self._check_hs2_rpc_status(resp.status)
      except Exception as e:
        log_exception_with_timestamp(e, "Warning",
           "close session RPC failed: {0}".format(type(e)), stderr_flag=False)
      self.session_handle = None
    self._close_transport()

  def _populate_query_options(self):
    # List all of the query options and their levels.
    # Retrying "set all" should be idempotent
    num_tries = 1
    while num_tries <= self.max_tries:
      raise_error = (num_tries == self.max_tries)
      set_all_handle = None
      if self.max_tries > 1:
        retry_msg = 'Num remaining tries: {0}'.format(self.max_tries - num_tries)
      else:
        retry_msg = ''
      try:
        set_all_handle = self.execute_query("set all", {})
        self.default_query_options = {}
        self.query_option_levels = {}
        for rows in self.fetch(set_all_handle):
          for name, value, level in rows:
            self.default_query_options[name.upper()] = value
            self.query_option_levels[name.upper()] = QueryOptionLevels.from_string(level)
        break
      except (QueryCancelledByShellException, MissingThriftMethodException,
        QueryStateException):
        raise
      except RPCException as r:
        if (r.exception_type == RPC_EXCEPTION_TAPPLICATION
            or r.exception_type == RPC_EXCEPTION_SERVER):
          raise
        log_exception_with_timestamp(r, "Exception",
           "type={0} when listing query options. {1}".format(type(r), retry_msg))
        if raise_error:
          raise
      except Exception as e:
        log_exception_with_timestamp(e, "Exception",
           "type={0} when listing query options. {1}".format(type(e), retry_msg))
        if raise_error:
          raise
      finally:
        if set_all_handle is not None:
          self.close_query(set_all_handle)

      time.sleep(self._get_sleep_interval_for_retries(num_tries))
      num_tries += 1

  def _ping_impala_service(self):
    def PingImpalaHS2Service(req):
      return self.imp_service.PingImpalaHS2Service(req)
    # PingImpalaHS2Service rpc is idempotent and so safe to retry.
    req = TPingImpalaHS2ServiceReq(self.session_handle)
    resp = self._do_hs2_rpc(PingImpalaHS2Service, req, retry_on_error=True)
    self._check_hs2_rpc_status(resp.status)
    self.webserver_address = resp.webserver_address
    return (resp.version, resp.webserver_address)

  def _create_query_req(self, query_str, set_query_options):
    conf_overlay = {}
    if sys.version_info.major < 3:
      key_value_pairs = set_query_options.iteritems()
    else:
      key_value_pairs = set_query_options.items()
    for k, v in key_value_pairs:
      conf_overlay[utf8_encode_if_needed(k)] = utf8_encode_if_needed(v)
    query = TExecuteStatementReq(sessionHandle=self.session_handle,
        statement=utf8_encode_if_needed(query_str),
        confOverlay=conf_overlay, runAsync=True)
    return query

  def execute_query(self, query_str, set_query_options):
    """Execute the query 'query_str' asynchronously on the server with options dictionary
    'set_query_options' and return a query handle that can be used for subsequent
    ImpalaClient method calls for the query."""
    self._clear_current_query_handle()
    self.is_query_cancelled = False

    def ExecuteStatement(req):
      return self.imp_service.ExecuteStatement(req)
    # Read queries should be idempotent but most dml queries are not. Also retrying
    # query execution from client could be expensive and so likely makes sense to do
    # it if server is also aware of the retries.
    req = self._create_query_req(query_str, set_query_options)
    resp = self._do_hs2_rpc(ExecuteStatement, req)
    if resp.status.statusCode != TStatusCode.SUCCESS_STATUS:
      msg = utf8_decode_if_needed(resp.status.errorMessage)
      raise QueryStateException("ERROR: {0}".format(msg))
    handle = resp.operationHandle

    try:
      self._set_current_query_handle(handle)
      if handle.hasResultSet:
        def GetResultSetMetadata(req):
          return self.imp_service.GetResultSetMetadata(req)
        # GetResultSetMetadata rpc is idempotent and should be safe to retry.
        req = TGetResultSetMetadataReq(handle)
        resp = self._do_hs2_rpc(GetResultSetMetadata, req, retry_on_error=True)
        self._check_hs2_rpc_status(resp.status)
        assert resp.schema is not None, resp
        # Attach the schema to the handle for convenience.
        handle.schema = resp.schema
      handle.is_closed = False
      return handle
    finally:
      self._clear_current_query_handle()

  def get_query_id_str(self, last_query_handle):
    if last_query_handle is None:
      return None

    guid_bytes = last_query_handle.operationId.guid
    return self._convert_id_to_str(guid_bytes)

  def _set_current_query_handle(self, query_handle):
    self._current_query_handle = query_handle

  def _clear_current_query_handle(self):
    self._current_query_handle = None

  def get_session_id(self):
    if self.session_handle is None:
      return None

    return self._convert_id_to_str(self.session_handle.sessionId.guid)

  def _convert_id_to_str(self, id_bytes):
    # The binary representation is present in the query handle but we need to
    # massage it into the expected string representation. C++ and Java code
    # treats the low and high half as two 64-bit little-endian integers and
    # as a result prints the hex representation in the reverse order to how
    # bytes are laid out in guid.
    low_bytes_reversed = id_bytes[7::-1]
    high_bytes_reversed = id_bytes[16:7:-1]

    if sys.version_info.major < 3:
      low_hex = low_bytes_reversed.encode('hex_codec')
      high_hex = high_bytes_reversed.encode('hex_codec')
    else:
      low_hex = low_bytes_reversed.hex()
      high_hex = high_bytes_reversed.hex()

    return "{low}:{high}".format(low=low_hex, high=high_hex)

  def fetch(self, query_handle):
    try:
      self._set_current_query_handle(query_handle)
      assert query_handle.hasResultSet
      prim_types = [column.typeDesc.types[0].primitiveEntry.type
                    for column in query_handle.schema.columns]
      column_value_getters = [HS2_VALUE_GETTERS[prim_type]
                          for prim_type in prim_types]
      column_value_converters = [self.value_converter.get_converter(prim_type)
                          for prim_type in prim_types]
      while True:
        def FetchResults(req):
          return self.imp_service.FetchResults(req)
        # FetchResults rpc is not idempotent unless the client and server communicate and
        # results are kept around for retry to be successful.
        req = TFetchResultsReq(query_handle,
                               TFetchOrientation.FETCH_NEXT,
                               self.fetch_size)
        resp = self._do_hs2_rpc(FetchResults, req)
        self._check_hs2_rpc_status(resp.status)

        # Transpose the columns into a row-based format for more convenient processing
        # for the display code. This is somewhat inefficient, but performance is
        # comparable to the old Beeswax code.
        yield self._transpose(column_value_getters, column_value_converters,
                              resp.results.columns)
        if not self._hasMoreRows(resp, column_value_getters):
          return
    finally:
      self._clear_current_query_handle()

  def _hasMoreRows(self, resp, column_value_getters):
    return resp.hasMoreRows

  def _transpose(self, column_value_getters, column_value_converters, columns):
    """Transpose the columns from a TFetchResultsResp into the row format returned
    by fetch() with all the values converted into their string representations for
    display. Uses the getters and convertes provided in column_value_getters[i] and
    column_value_converters[i] for column i."""
    tcols = [column_value_getters[i](col) for i, col in enumerate(columns)]
    num_rows = len(tcols[0].values)
    # Preallocate rows for efficiency.
    rows = [[None] * len(tcols) for i in xrange(num_rows)]
    for col_idx, tcol in enumerate(tcols):
      is_null = bitarray(endian='little')
      is_null.frombytes(tcol.nulls)
      stringifier = column_value_converters[col_idx]
      # Skip stringification if not needed. This makes large extracts of tpch.orders
      # ~8% faster according to benchmarks.
      if stringifier is None:
        bitset_len = min(len(is_null), len(rows))
        for current_row in xrange(bitset_len):
          rows[current_row][col_idx] = 'NULL' if is_null[current_row] \
              else tcol.values[current_row]
        for current_row in xrange(bitset_len, len(rows)):
          rows[current_row][col_idx] = tcol.values[current_row]
      else:
        bitset_len = min(len(is_null), len(rows))
        for current_row in xrange(bitset_len):
          rows[current_row][col_idx] = 'NULL' if is_null[current_row] \
              else stringifier(tcol.values[current_row])
        for current_row in xrange(bitset_len, len(rows)):
          rows[current_row][col_idx] = stringifier(tcol.values[current_row])
    return rows

  def close_dml(self, last_query_handle):
    try:
      self._set_current_query_handle(last_query_handle)

      def CloseImpalaOperation(req):
        return self.imp_service.CloseImpalaOperation(req)
      # CloseImpalaOperation rpc is not idempotent for dmls.
      req = TCloseImpalaOperationReq(last_query_handle)
      resp = self._do_hs2_rpc(CloseImpalaOperation, req)
      self._check_hs2_rpc_status(resp.status)
      if not resp.dml_result:
        raise RPCException("Impala DML operation did not return DML statistics.")
      last_query_handle.is_closed = True
      return self._process_dml_result(resp.dml_result)
    finally:
      self._clear_current_query_handle()

  def close_query(self, last_query_handle):
    try:
      self._set_current_query_handle(last_query_handle)
      # Set a member in the handle to make sure that it is idempotent
      if last_query_handle.is_closed:
        return True

      def CloseImpalaOperation(req):
        return self.imp_service.CloseImpalaOperation(req)
      # CloseImpalaOperation rpc is idempotent for non dml queries and so safe to retry.
      req = TCloseImpalaOperationReq(last_query_handle)
      resp = self._do_hs2_rpc(CloseImpalaOperation, req, retry_on_error=True)
      last_query_handle.is_closed = True
      return self._is_hs2_nonerror_status(resp.status.statusCode)
    finally:
      self._clear_current_query_handle()

  def cancel_query(self, last_query_handle):
    # Cancel sets query_state to ERROR_STATE before calling cancel() in the
    # co-ordinator, so we don't need to wait.
    try:
      self._set_current_query_handle(last_query_handle)
      if last_query_handle.is_closed:
        return True

      def CancelOperation(req):
        return self.imp_service.CancelOperation(req)
      # CancelOperation rpc is idempotent and so safe to retry.
      req = TCancelOperationReq(last_query_handle)
      resp = self._do_hs2_rpc(CancelOperation, req, retry_on_error=True)
      return self._is_hs2_nonerror_status(resp.status.statusCode)
    finally:
      self._clear_current_query_handle()

  def get_query_state(self, last_query_handle):
    try:
      self._set_current_query_handle(last_query_handle)

      def GetOperationStatus(req):
        return self.imp_service.GetOperationStatus(req)
      # GetOperationStatus rpc is idempotent and so safe to retry.
      req = TGetOperationStatusReq(last_query_handle)
      resp = self._do_hs2_rpc(GetOperationStatus, req, retry_on_error=True)
      self._check_hs2_rpc_status(resp.status)
      return resp.operationState
    finally:
      self._clear_current_query_handle()

  def get_runtime_profile(self, last_query_handle):
    try:
      self._set_current_query_handle(last_query_handle)

      def GetRuntimeProfile(req):
        return self.imp_service.GetRuntimeProfile(req)
      # GetRuntimeProfile rpc is idempotent and so safe to retry.
      profile_req = TGetRuntimeProfileReq(last_query_handle,
                                          self.session_handle,
                                          include_query_attempts=True)
      resp = self._do_hs2_rpc(GetRuntimeProfile, profile_req, retry_on_error=True)
      self._check_hs2_rpc_status(resp.status)
      failed_profile = None
      if resp.failed_profiles and len(resp.failed_profiles) >= 1:
        failed_profile = resp.failed_profiles[0]
      return resp.profile, failed_profile
    finally:
      self._clear_current_query_handle()

  def get_summary(self, last_query_handle):
    try:
      self._set_current_query_handle(last_query_handle)

      def GetExecSummary(req):
        return self.imp_service.GetExecSummary(req)
      # GetExecSummary rpc is idempotent and so safe to retry.
      req = TGetExecSummaryReq(last_query_handle,
                           self.session_handle,
                           include_query_attempts=True)
      resp = self._do_hs2_rpc(GetExecSummary, req, retry_on_error=True)
      self._check_hs2_rpc_status(resp.status)
      failed_summary = None
      if resp.failed_summaries and len(resp.failed_summaries) >= 1:
        failed_summary = resp.failed_summaries[0]
      return resp.summary, failed_summary
    finally:
      self._clear_current_query_handle()

  def get_column_names(self, last_query_handle):
    # The handle has the schema embedded in it.
    assert last_query_handle.hasResultSet
    return [column.columnName for column in last_query_handle.schema.columns]

  def expect_result_metadata(self, query_str, query_handle):
    """ Given a query string, return True if impalad expects result metadata."""
    return query_handle.hasResultSet

  def _get_warn_or_error_log(self, last_query_handle, warn):
    """Returns all messages from the error log prepended with 'WARNINGS:' or 'ERROR:' for
    last_query_handle, depending on whether warn is True or False. Note that the error
    log may contain messages that are not errors (e.g. warnings)."""
    try:
      self._set_current_query_handle(last_query_handle)
      if last_query_handle is None:
        return "Query could not be executed"

      def GetLog(req):
        return self.imp_service.GetLog(req)
      # GetLog rpc is idempotent and so safe to retry.
      req = TGetLogReq(last_query_handle)
      resp = self._do_hs2_rpc(GetLog, req, retry_on_error=True)
      self._check_hs2_rpc_status(resp.status)

      log = utf8_decode_if_needed(resp.log)

      # Strip progress message out of HS2 log.
      log = HS2_LOG_PROGRESS_REGEX.sub("", log)
      if log and log.strip():
        log = self._append_retried_query_link(log)
        type_str = "WARNINGS" if warn is True else "ERROR"
        return "%s: %s" % (type_str, log)
      return ""
    finally:
      self._clear_current_query_handle()

  def _do_hs2_rpc(self, rpc, rpc_input,
                  suppress_error_on_cancel=True, retry_on_error=False):
    """Executes the provided 'rpc' callable and translates any exceptions in the
    appropriate exception for the shell. The input 'rpc' must be a python function
    with the __name__ attribute and not a lambda function. Exceptions raised include:
    * DisconnectedException if the client cannot communicate with the server.
    * QueryCancelledByShellException if 'suppress_error_on_cancel' is true, the RPC
      fails and the query was cancelled from the shell (i.e. self.is_query_cancelled).
    * MissingThriftMethodException if the thrift method is not implemented on the server.
    Does not validate any status embedded in the returned RPC message.
    If 'retry_on_error' is true, the rpc is retried if an exception is raised. The maximum
    number of tries is determined by 'self.max_tries'. Retries, if enabled, are attempted
    for all exceptions other than TApplicationException."""

    self._request_num += 1
    self._current_request_id = "{0}-{1}".format(self._base_request_id, self._request_num)
    self._check_connected()
    num_tries = 1
    max_tries = num_tries
    if retry_on_error:
      max_tries = self.max_tries
    while num_tries <= max_tries:
      start_time = self._print_rpc_start(rpc, rpc_input, num_tries)
      raise_error = (num_tries == max_tries)
      # Generate a retry message, only if retries and supported.
      will_retry = False
      retry_secs = None
      if retry_on_error and self.max_tries > 1:
        retry_msg = 'Num remaining tries: {0}'.format(max_tries - num_tries)
        if num_tries < max_tries:
          will_retry = True
      else:
        retry_msg = ''
      try:
        rpc_output = rpc(rpc_input)
        self._print_rpc_end(rpc, rpc_output, start_time, "SUCCESS")
        return rpc_output
      except TTransportException as e:
        # Unwrap socket.error so we can handle it directly.
        if isinstance(e.inner, socket.error):
          e = e.inner
        # issue with the connection with the impalad
        log_exception_with_timestamp(e, "Exception",
           "type={0} in {1}. {2}".format(type(e), rpc.__name__, retry_msg))
        self._print_rpc_end(rpc, None, start_time, "Error - TTransportException")
        if raise_error:
          if isinstance(e, TTransportException):
            raise DisconnectedException("Error communicating with impalad: %s" % e)
          raise e
      except TApplicationException as t:
        self._print_rpc_end(rpc, None, start_time, "Error - TApplicationException")
        # Suppress the errors from cancelling a query that is in waiting_to_finish state
        if suppress_error_on_cancel and self.is_query_cancelled:
          raise QueryCancelledByShellException()
        if t.type == TApplicationException.UNKNOWN_METHOD:
          raise MissingThriftMethodException(t.message)
        raise RPCException("Application Exception : {0}".format(t),
          RPC_EXCEPTION_TAPPLICATION)
      except HttpError as h:
        if will_retry:
          retry_after = h.http_headers.get('Retry-After', None)
          if retry_after:
            try:
              retry_secs = int(retry_after)
            except ValueError:
              retry_secs = None
        if retry_secs:
          log_exception_with_timestamp(h, "Exception",
              "type={0} in {1}. {2}, retry after {3} secs"
              .format(type(h), rpc.__name__, retry_msg, retry_secs))
        else:
          log_exception_with_timestamp(h, "Exception", "type={0} in {1}. {2}"
                .format(type(h), rpc.__name__, retry_msg))
        self._print_rpc_end(rpc, None, start_time, "Error - HttpError")
        if raise_error:
          raise
      except Exception as e:
        log_exception_with_timestamp(e, "Exception", "type={0} in {1}. {2}"
          .format(type(e), rpc.__name__, retry_msg))
        self._print_rpc_end(rpc, None, start_time, "Error")
        if raise_error:
          raise
      if retry_secs:
        time.sleep(retry_secs)
      else:
        time.sleep(self._get_sleep_interval_for_retries(num_tries))
      num_tries += 1

  def _check_hs2_rpc_status(self, status):
    """If the TCLIService.TStatus 'status' is an error status the raise an exception
    with an appropriate error message. The exceptions raised are:
    * QueryCancelledByShellException if the RPC fails and the query was cancelled from
      the shell (i.e. self.is_query_cancelled).
    * QueryStateException if the query is not registered on the server.
    * RPCException in all other cases."""
    if status.statusCode == TStatusCode.ERROR_STATUS:
      # Suppress the errors from cancelling a query that is in fetch state
      if self.is_query_cancelled:
        raise QueryCancelledByShellException()
      raise RPCException("ERROR: {0}".format(status.errorMessage),
        RPC_EXCEPTION_SERVER)
    elif status.statusCode == TStatusCode.INVALID_HANDLE_STATUS:
      if self.is_query_cancelled:
        raise QueryCancelledByShellException()
      raise QueryStateException('Error: Stale query handle')
    else:
      # Treat all non-error codes as success.
      assert self._is_hs2_nonerror_status(status.statusCode), status.statusCode

  def _is_hs2_nonerror_status(self, status_code):
    """Return whether 'status_code' is a non-error TStatusCode value."""
    return status_code in (TStatusCode.SUCCESS_STATUS,
                           TStatusCode.SUCCESS_WITH_INFO_STATUS,
                           TStatusCode.STILL_EXECUTING_STATUS)

  def _print_line_separator(self, fh):
    """Prints out a visible separator suitable for outputting debug and
    trace information to the screen or a file"""
    fh.write("------------------------------------------------")
    fh.write("------------------------------------------------\n")

  def _print_rpc_start(self, rpc_func, rpc_input, num_tries):
    """Prints out a nicely formatted detailed breakdown of the request to
    a rpc call.  Handles both the 'rpc_stdout' and 'rpc_file' command line arguments."""
    if self.rpc_stdout or self.rpc_file is not None:
      start_time = datetime.now()

      def print_start_to_file(fh):
        self._print_line_separator(fh)
        fh.write("[{0}] RPC CALL STARTED:\n".format(start_time))
        fh.write("OPERATION: {0}\nDETAILS:\n".format(rpc_func.__name__))
        fh.write("  * Impala Session Id: {0}\n".format(self.get_session_id()))
        fh.write("  * Impala Query Id:   {0}\n"
                  .format(self.get_query_id_str(self._current_query_handle)))
        fh.write("  * Attempt Count:     {0}\n".format(num_tries))
        fh.write("\nRPC REQUEST:\n")
        self.thrift_printer.print_obj(rpc_input, fh)
        self._print_line_separator(fh)

      if self.rpc_stdout:
        print_start_to_file(sys.stdout)

      if self.rpc_file:
        with open(self.rpc_file, "a") as f:
          print_start_to_file(f)

      return start_time

    return None

  def _print_rpc_end(self, rpc_func, rpc_output, start_time, result):
    """Prints out a nicely formatted detailed breakdown of the response from
    a rpc call.  Handles both the 'rpc_stdout' and 'rpc_file' command line arguments."""
    if self.rpc_stdout or self.rpc_file is not None:
      end_time = datetime.now()
      duration = end_time - start_time

      def print_end_to_file(fh):
        self._print_line_separator(fh)
        fh.write("[{0}] RPC CALL FINISHED:\n".format(end_time))
        fh.write("OPERATION: {0}\nDETAILS:\n".format(rpc_func.__name__))
        fh.write("  * Time:   {0}ms\n".format(duration.total_seconds() * 1000))
        fh.write("  * Result: {0}\n".format(result))
        if rpc_output is not None:
          fh.write("\nRPC RESPONSE:\n")
          self.thrift_printer.print_obj(rpc_output, fh)
        self._print_line_separator(fh)

      if self.rpc_stdout:
        print_end_to_file(sys.stdout)

      if self.rpc_file:
        with open(self.rpc_file, "a") as f:
          print_end_to_file(f)


class RpcStatus:
  """Convenience enum used in ImpalaBeeswaxClient to describe Rpc return statuses"""
  OK = 0
  ERROR = 1


class StrictHS2Client(ImpalaHS2Client):
  """HS2 client. Uses the HS2 protocol without Impala-specific extensions.
  This can be used to connect with HiveServer2 directly."""
  def __init__(self, *args, **kwargs):
    super(StrictHS2Client, self).__init__(*args, **kwargs)

  def close_dml(self, last_query_handle):
    self.close_query(last_query_handle)
    return (None, None, None)

  def close_query(self, last_query_handle):
    # Set a member in the handle to make sure that it is idempotent
    if last_query_handle.is_closed:
      return True

    try:
      self._set_current_query_handle(last_query_handle)

      def CloseOperation(req):
        return self.imp_service.CloseOperation(req)
      req = TCloseOperationReq(last_query_handle)
      resp = self._do_hs2_rpc(CloseOperation, req, retry_on_error=False)
      last_query_handle.is_closed = True
      return self._is_hs2_nonerror_status(resp.status.statusCode)
    finally:
      self._clear_current_query_handle()

  def _ping_impala_service(self):
    return ("N/A", "N/A")

  def get_warning_log(self, last_query_handle):
    return ""

  def get_error_log(self, last_query_handle):
    return ""

  def get_runtime_profile(self, last_query_handle):
    return None, None

  def _populate_query_options(self):
    return

  def _hasMoreRows(self, resp, column_value_getters):
    tcol = column_value_getters[0](resp.results.columns[0])
    return len(tcol.values)


class ImpalaBeeswaxClient(ImpalaClient):
  """Legacy Beeswax client. Uses the Beeswax protocol plus Impala-specific extensions.
  TODO: remove once we've phased out beeswax."""
  def __init__(self, *args, **kwargs):
    super(ImpalaBeeswaxClient, self).__init__(*args, **kwargs)
    assert not self.use_http_base_transport
    self.FINISHED_STATE = QueryState._NAMES_TO_VALUES["FINISHED"]
    self.ERROR_STATE = QueryState._NAMES_TO_VALUES["EXCEPTION"]
    self.CANCELED_STATE = QueryState._NAMES_TO_VALUES["EXCEPTION"]

  def _get_thrift_client(self, protocol):
    return ImpalaService.Client(protocol)

  def _options_to_string_list(self, set_query_options):
    if sys.version_info.major < 3:
      key_value_pairs = set_query_options.iteritems()
    else:
      key_value_pairs = set_query_options.items()
    return [utf8_encode_if_needed("%s=%s" % (k, v)) for (k, v) in key_value_pairs]

  def _open_session(self):
    # Beeswax doesn't have a "session" concept independent of connections, so
    # we do not need to explicitly open a sesion. We still need to set up the
    # query options.
    #
    # The default query options are retrieved from a rpc call, and are dependent
    # on the impalad to which a connection has been established. They need to be
    # refreshed each time a connection is made. This is particularly helpful when
    # there is a version mismatch between the shell and the impalad.
    try:
      get_default_query_options = self.imp_service.get_default_configuration(False)
    except Exception:
      return
    rpc_result = self._do_beeswax_rpc(lambda: get_default_query_options)
    options, status = rpc_result
    if status != RpcStatus.OK:
      raise RPCException("Unable to retrieve default query options")

    for option in options:
      self.default_query_options[option.key.upper()] = option.value
      # If connected to an Impala that predates IMPALA-2181 then the received options
      # wouldn't contain a level attribute. In this case the query_option_levels
      # map is left empty.
      if option.level is not None:
        self.query_option_levels[option.key.upper()] = option.level

  def close_connection(self):
    # Beeswax sessions are scoped to the connection, so we only need to close transport.
    self._close_transport()

  def _ping_impala_service(self):
    try:
      resp = self.imp_service.PingImpalaService()
    except TApplicationException as t:
      if t.type == TApplicationException.UNKNOWN_METHOD:
        raise MissingThriftMethodException(t.message)
      raise
    except TTransportException as e:
      # Unwrap socket.error so we can handle it directly.
      if isinstance(e.inner, socket.error):
        raise e.inner
      raise DisconnectedException("Error communicating with impalad: %s" % e)
    self.webserver_address = resp.webserver_address
    return (resp.version, resp.webserver_address)

  def _create_query_req(self, query_str, set_query_options):
    query = BeeswaxService.Query()
    query.hadoop_user = self.user
    query.query = utf8_encode_if_needed(query_str)
    query.configuration = self._options_to_string_list(set_query_options)
    return query

  def execute_query(self, query_str, set_query_options):
    """Execute the query 'query_str' asynchronously on the server with options dictionary
    'set_query_options' and return a query handle that can be used for subsequent
    ImpalaClient method calls for the query."""
    query = self._create_query_req(query_str, set_query_options)
    self.is_query_cancelled = False
    handle, rpc_status = self._do_beeswax_rpc(lambda: self.imp_service.query(query))
    if rpc_status != RpcStatus.OK:
      raise RPCException("Error executing the query")
    handle.is_closed = False
    return handle

  def get_query_id_str(self, last_query_handle):
    return last_query_handle.id

  def get_query_state(self, last_query_handle):
    state, rpc_status = self._do_beeswax_rpc(
        lambda: self.imp_service.get_state(last_query_handle))
    if rpc_status != RpcStatus.OK:
      return self.ERROR_STATE
    return state

  def fetch(self, query_handle):
    while True:
      result, rpc_status = self._do_beeswax_rpc(
         lambda: self.imp_service.fetch(query_handle, False,
                                        self.fetch_size))
      if rpc_status != RpcStatus.OK:
        raise RPCException()

      def split_row_and_decode_if_needed(row):
        # Decode before splitting as this can remove incidental tabs from
        # multibyte characters.
        return utf8_decode_if_needed(row).split('\t')

      yield [split_row_and_decode_if_needed(row) for row in result.data]

      if not result.has_more:
        return

  def close_dml(self, last_query_handle):
    dml_result, rpc_status = self._do_beeswax_rpc(
        lambda: self.imp_service.CloseInsert(last_query_handle))
    if rpc_status != RpcStatus.OK:
      raise RPCException()
    last_query_handle.is_closed = True
    return self._process_dml_result(dml_result)

  def close_query(self, last_query_handle):
    # Set a member in the handle to make sure that it is idempotent
    if last_query_handle.is_closed:
      return True
    _, rpc_status = self._do_beeswax_rpc(
        lambda: self.imp_service.close(last_query_handle))
    last_query_handle.is_closed = True
    return rpc_status == RpcStatus.OK

  def cancel_query(self, last_query_handle):
    # Cancel sets query_state to ERROR_STATE before calling cancel() in the
    # co-ordinator, so we don't need to wait.
    if last_query_handle.is_closed:
      return True
    _, rpc_status = self._do_beeswax_rpc(
        lambda: self.imp_service.Cancel(last_query_handle), False)
    return rpc_status == RpcStatus.OK

  def get_runtime_profile(self, last_query_handle):
    profile, rpc_status = self._do_beeswax_rpc(
        lambda: self.imp_service.GetRuntimeProfile(last_query_handle))
    if rpc_status == RpcStatus.OK and profile:
      return profile, None
    return None, None

  def get_summary(self, last_query_handle):
    summary, rpc_status = self._do_beeswax_rpc(
      lambda: self.imp_service.GetExecSummary(last_query_handle))
    if rpc_status == RpcStatus.OK and summary:
      return summary, None
    return None, None

  def get_column_names(self, last_query_handle):
    # Note: the code originally ignored the RPC status. don't mess with it.
    metadata, _ = self._do_beeswax_rpc(
        lambda: self.imp_service.get_results_metadata(last_query_handle))
    if metadata is not None:
      return [fs.name for fs in metadata.schema.fieldSchemas]

  def expect_result_metadata(self, query_str, query_handle):
    # Beeswax doesn't provide us this metadata; try to guess whether to expect it based
    # on the query string.
    query_substr = query_str[:3].lower()
    excluded_query_types = ['use']
    if any(map(query_substr.startswith, excluded_query_types)):
      return False
    return True

  def _get_warn_or_error_log(self, last_query_handle, warn):
    if last_query_handle is None:
      return "Query could not be executed"
    log, rpc_status = self._do_beeswax_rpc(
        lambda: self.imp_service.get_log(last_query_handle.log_context))
    if rpc_status != RpcStatus.OK:
      type_str = "warn" if warn is True else "error"
      return "Failed to get %s log: %s" % (type_str, rpc_status)
    if log and log.strip():
      log = utf8_decode_if_needed(log)
      log = self._append_retried_query_link(log)
      type_str = "WARNINGS" if warn is True else "ERROR"
      return "%s: %s" % (type_str, log)
    return ""

  def _do_beeswax_rpc(self, rpc, suppress_error_on_cancel=True):
    """Executes the provided 'rpc' callable. Raises exceptions for most errors,
    including:
    * DisconnectedException if the client cannot communicate with the server.
    * QueryCancelledByShellException if 'suppress_error_on_cancel' is true, the RPC
      fails and the query was cancelled from the shell (i.e. self.is_query_cancelled).
    * RPCException if the operation fails with an error status
    * QueryStateException if the query is not registered on the server.
    * MissingThriftMethodException if the thrift method is not implemented on the server.
    Returns RPCStatus.OK on success or RPCStatus.ERROR for any other errors."""
    self._check_connected()
    try:
      ret = rpc()
      status = RpcStatus.OK
      # TODO: In the future more advanced error detection/handling can be done based on
      # the TStatus return value. For now, just print any error(s) that were encountered
      # and validate the result of the operation was a success.
      if ret is not None and isinstance(ret, TStatus):
        if ret.status_code != TErrorCode.OK:
          if ret.error_msgs:
            raise RPCException('RPC Error: %s' % '\n'.join(ret.error_msgs))
          status = RpcStatus.ERROR
      return ret, status
    except BeeswaxService.QueryNotFoundException:
      if suppress_error_on_cancel and self.is_query_cancelled:
        raise QueryCancelledByShellException()
      raise QueryStateException('Error: Stale query handle')
    # beeswaxException prints out the entire object, printing
    # just the message is far more readable/helpful.
    except BeeswaxService.BeeswaxException as b:
      # Suppress the errors from cancelling a query that is in fetch state
      if suppress_error_on_cancel and self.is_query_cancelled:
        raise QueryCancelledByShellException()
      raise RPCException(utf8_encode_if_needed("ERROR: %s") % b.message)
    except TTransportException as e:
      # Unwrap socket.error so we can handle it directly.
      if isinstance(e.inner, socket.error):
        raise e.inner
      # issue with the connection with the impalad
      raise DisconnectedException("Error communicating with impalad: %s" % e)
    except TApplicationException as t:
      # Suppress the errors from cancelling a query that is in waiting_to_finish state
      if suppress_error_on_cancel and self.is_query_cancelled:
        raise QueryCancelledByShellException()
      if t.type == TApplicationException.UNKNOWN_METHOD:
        raise MissingThriftMethodException(t.message)
      raise RPCException("Application Exception : %s" % t)
    except Exception as e:
      # This final except clause should ONLY be exercised in the case of Impala
      # shell being installed as a standalone python package from public PyPI,
      # rather than being included as part of a typical Impala deployment.
      #
      # TODO: Revisit the following:
      # Essentially, it's a hack that is required due to issues stemming from
      # IMPALA-6808. Because of the way the Impala python environment has been
      # somewhat haphazardly constructed, we end up polluting the top level Impala
      # python environment with modules that should really be sub-modules. One of
      # the principal places this occurs is with the various modules required by
      # the Impala shell. This isn't a concern when the shell is invoked via a
      # specially installed version of python that belongs to Impala, but it does
      # become an issue when the shell is being run using the system python.
      #
      # When we install the shell as a standalone package, we need to construct
      # it in such a way that all of the internal modules are contained within
      # a top-level impala_shell namespace. However, this then breaks various
      # imports and, in this case, exception handling in the original code.
      # As far as I can tell, there's no clean way to address this without fully
      # resolving IMPALA-6808.
      #
      # Without taking some additional measure here to recognize certain common
      # exceptions, especially Beeswax exceptions raised by RPC calls, when
      # errors occur during a standalone shell session, we wind up falling
      # entirely through this block and returning nothing to the caller (which
      # happens to be the primary command loop in impala_shell.py). This in turn
      # has the result of disconnecting the shell in the case of, say, even simple
      # typos in database or table names.
      if suppress_error_on_cancel and self.is_query_cancelled:
        raise QueryCancelledByShellException()
      else:
        if "BeeswaxException" in str(e):
          raise RPCException("ERROR: %s" % e.message)
        if "QueryNotFoundException" in str(e):
          raise QueryStateException('Error: Stale query handle')
        # Print more details for other kinds of exceptions
        log_exception_with_timestamp(e, "Exception", "type={0}".format(type(e)))
        traceback.print_exc()
        raise Exception("Encountered unknown exception")


def log_exception_with_timestamp(e, type="Exception", msg="", stderr_flag=True):
  # method log_exception_with_timestamp prints timestamp with exception trace
  # and accepts custom message before timestamp. stderr_flag controls print statement
  # to be logged in stderr, by default it is true.
  if (stderr_flag):
      print("%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), type,
      msg), e, file=sys.stderr)
  else:
      print("%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), type,
       msg), e)


def log_timestamp(type="Exception", msg=""):
  # method log_timestamp prints timestamp with custom message
  print("%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), type, msg),
      file=sys.stderr)
