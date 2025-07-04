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

package org.apache.impala.planner;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import org.apache.impala.analysis.Analyzer;
import org.apache.impala.analysis.BinaryPredicate;
import org.apache.impala.analysis.Expr;
import org.apache.impala.analysis.ExprSubstitutionMap;
import org.apache.impala.analysis.JoinOperator;
import org.apache.impala.analysis.TableSampleClause;
import org.apache.impala.catalog.Type;
import org.apache.impala.common.ImpalaException;
import org.apache.impala.common.Pair;
import org.apache.impala.common.ThriftSerializationCtx;
import org.apache.impala.thrift.TExplainLevel;
import org.apache.impala.thrift.TIcebergDeleteNode;
import org.apache.impala.thrift.TPlanNode;
import org.apache.impala.thrift.TPlanNodeType;
import org.apache.impala.thrift.TQueryOptions;
import org.apache.impala.util.MathUtil;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.google.common.base.MoreObjects;
import com.google.common.base.Preconditions;
import org.apache.impala.util.ExprUtil;

public class IcebergDeleteNode extends JoinNode {
  private final static Logger LOG = LoggerFactory.getLogger(IcebergDeleteNode.class);
  public IcebergDeleteNode(
      PlanNode outer, PlanNode inner, List<BinaryPredicate> eqJoinConjuncts) {
    super(outer, inner, true, DistributionMode.NONE, JoinOperator.ICEBERG_DELETE_JOIN,
        eqJoinConjuncts, Collections.emptyList(), "ICEBERG DELETE");
    Preconditions.checkNotNull(eqJoinConjuncts);
    Preconditions.checkState(joinOp_ == JoinOperator.ICEBERG_DELETE_JOIN);
    Preconditions.checkState(conjuncts_.isEmpty());
    Preconditions.checkState(runtimeFilters_.isEmpty());
  }

  @Override
  public boolean isBlockingJoinNode() {
    return true;
  }

  @Override
  public List<BinaryPredicate> getEqJoinConjuncts() {
    return eqJoinConjuncts_;
  }

  @Override
  public void init(Analyzer analyzer) throws ImpalaException {
    super.init(analyzer);
    List<BinaryPredicate> newEqJoinConjuncts = new ArrayList<>();
    ExprSubstitutionMap combinedChildSmap = getCombinedChildSmap();
    for (Expr c : eqJoinConjuncts_) {
      BinaryPredicate eqPred =
          (BinaryPredicate) c.substitute(combinedChildSmap, analyzer, false);
      Type t0 = eqPred.getChild(0).getType();
      Type t1 = eqPred.getChild(1).getType();
      Preconditions.checkState(t0.matchesType(t1));
      BinaryPredicate newEqPred =
          new BinaryPredicate(eqPred.getOp(), eqPred.getChild(0), eqPred.getChild(1));
      newEqPred.analyze(analyzer);
      newEqJoinConjuncts.add(newEqPred);
    }
    eqJoinConjuncts_ = newEqJoinConjuncts;
    orderJoinConjunctsByCost();
    computeStats(analyzer);
  }

  @Override
  public void computeStats(Analyzer analyzer) {
    super.computeStats(analyzer);
    // Compute cardinality differently. Let's assume all position delete records apply to
    // a data record (Concurrent DELETEs should be extremely rare).
    // Also assume that the left side's selectivity applies to the delete records as well.
    // Please note that left side's cardinality already takes the selectivity into
    // account (i.e. no need to do leftSelectivity * leftCard).
    PlanNode leftChild = getChild(0);
    Preconditions.checkState(leftChild instanceof HdfsScanNode);
    HdfsScanNode leftScanChild = (HdfsScanNode) leftChild;
    TableSampleClause leftSampleParams = leftScanChild.getSampleParams();
    long leftCardWithSelectivity = leftScanChild.cardinality_;
    long rightCard = getChild(1).cardinality_;
    // Both sides should have non-negative cardinalities.
    Preconditions.checkState(leftCardWithSelectivity >= 0);
    Preconditions.checkState(rightCard >= 0);
    // The delete records on the right might refer to data records that are filtered
    // out by predicates or table sampling. Let's incorporate this into our cardinality
    // estimation.
    double leftSelectivity = leftScanChild.computeSelectivity();
    long effectiveRightCardinality = (long)(leftSelectivity * rightCard);
    double leftSampling = leftSampleParams == null ?
        1.0 : leftSampleParams.getPercentBytes() / 100.0;
    Preconditions.checkState(leftSampling >= 0);
    Preconditions.checkState(leftSampling <= 1.0);
    effectiveRightCardinality = (long)(effectiveRightCardinality * leftSampling);
    cardinality_ = Math.max(1, leftCardWithSelectivity - effectiveRightCardinality);
  }

  @Override
  protected String debugString() {
    return MoreObjects.toStringHelper(this)
        .add("eqJoinConjuncts_", eqJoinConjunctsDebugString())
        .addValue(super.debugString())
        .toString();
  }

  private String eqJoinConjunctsDebugString() {
    MoreObjects.ToStringHelper helper = MoreObjects.toStringHelper(this);
    for (Expr entry : eqJoinConjuncts_) {
      helper.add("lhs", entry.getChild(0)).add("rhs", entry.getChild(1));
    }
    return helper.toString();
  }

  @Override
  protected void toThrift(TPlanNode msg, ThriftSerializationCtx serialCtx) {
    msg.node_type = TPlanNodeType.ICEBERG_DELETE_NODE;
    msg.join_node = joinNodeToThrift(serialCtx);
    msg.join_node.iceberg_delete_node = new TIcebergDeleteNode();
    msg.join_node.iceberg_delete_node.setEq_join_conjuncts(
        getThriftEquiJoinConjuncts(serialCtx));
  }

  @Override
  protected String getNodeExplainString(
      String prefix, String detailPrefix, TExplainLevel detailLevel) {
    StringBuilder output = new StringBuilder();
    output.append(
        String.format("%s%s [%s]\n", prefix, getDisplayLabel(), getDisplayLabelDetail()));

    if (detailLevel.ordinal() > TExplainLevel.MINIMAL.ordinal()) {
      if (!isDeleteRowsJoin_
          || detailLevel.ordinal() >= TExplainLevel.EXTENDED.ordinal()) {
        output.append(detailPrefix + "equality predicates: ");
        for (int i = 0; i < eqJoinConjuncts_.size(); ++i) {
          Expr eqConjunct = eqJoinConjuncts_.get(i);
          output.append(eqConjunct.toSql());
          if (i + 1 != eqJoinConjuncts_.size()) output.append(", ");
        }
        output.append("\n");
      }
    }
    return output.toString();
  }

  /**
   * Helper method to compute the resource requirements for the join that can be
   * called from the builder or the join node. Returns a pair of the probe
   * resource requirements and the build resource requirements.
   */
  @Override
  public Pair<ResourceProfile, ResourceProfile> computeJoinResourceProfile(
      TQueryOptions queryOptions) {
    Preconditions.checkState(getChild(1).getCardinality() != -1);
    Preconditions.checkState(getChild(1).getAvgRowSize() != -1);
    Preconditions.checkState(fragment_.getNumNodes() > 0);

    long rhsCard = getChild(1).getCardinality();
    long rhsNdv = 1;
    // Calculate the ndv of the right child, which is the multiplication of
    // the ndv of the right child column
    for (Expr eqJoinPredicate : eqJoinConjuncts_) {
      long rhsPdNdv = getNdv(eqJoinPredicate.getChild(1));
      rhsPdNdv = Math.min(rhsPdNdv, rhsCard);
      if (rhsPdNdv != -1) rhsNdv = MathUtil.multiplyCardinalities(rhsNdv, rhsPdNdv);
    }

    // The memory of the data stored in hash table is the file path of the data files
    // which have delete files and roaring bitmaps that store the positions. Roaring
    // bitmaps store bitmaps in a compressed format, so 2 bytes per position is a
    // conservative estimate (actual mem usage is expected to be lower).
    PlanNode lhsNode = getChild(0);
    // Tuple caching can place a TupleCacheNode above the scan, so look past a
    // TupleCacheNode if it is present
    if (lhsNode instanceof TupleCacheNode) lhsNode = lhsNode.getChild(0);
    int numberOfDataFilesWithDelete = ((IcebergScanNode) lhsNode)
        .getFileDescriptorsWithLimit(null, false, -1).size();
    double avgFilePathLen = getChild(1).getAvgRowSize();
    long filePathsSize = (long) Math.ceil(numberOfDataFilesWithDelete * avgFilePathLen);
    // Number of bytes needed to store a file position in a RoaringBitmap.
    final int BYTES_PER_POSITION = 2;
    // Let's calculate with some overhead per roaring bitmap.
    final int PER_ROARING_BITMAP_OVERHEAD = 512;
    long roaringBitmapOverhead =
        PER_ROARING_BITMAP_OVERHEAD * numberOfDataFilesWithDelete;
    long roaringBitmapSize = (long) Math.ceil(BYTES_PER_POSITION * rhsCard) +
        roaringBitmapOverhead;
    long totalBuildDataBytes = filePathsSize + roaringBitmapSize;

    // In both modes, on average the data in the hash tables are distributed evenly
    // among the instances.
    int numBuildInstances = fragment_.getNumNodes();
    long perBuildInstanceMemEstimate = totalBuildDataBytes / numBuildInstances;

    // Almost all resource consumption is in the build, or shared between the build and
    // the probe. These are accounted for in the build profile.
    ResourceProfile probeProfile =
        new ResourceProfileBuilder().setMemEstimateBytes(0).build();
    ResourceProfile buildProfile =
        new ResourceProfileBuilder()
            .setMemEstimateBytes(perBuildInstanceMemEstimate)
            .setMinMemReservationBytes(perBuildInstanceMemEstimate)
            .build();
    return Pair.create(probeProfile, buildProfile);
  }

  @Override
  public Pair<ProcessingCost, ProcessingCost> computeJoinProcessingCost() {
    // Assume 'eqJoinConjuncts_' will be applied to all rows from lhs and rhs side.
    float eqJoinPredicateEvalCost = ExprUtil.computeExprsTotalCost(eqJoinConjuncts_);

    // Compute the processing cost for lhs.
    ProcessingCost probeProcessingCost =
        ProcessingCost.basicCost(getDisplayLabel() + " Probe side (eqJoinConjuncts_)",
            getProbeCardinalityForCosting(), eqJoinPredicateEvalCost);

    // Compute the processing cost for rhs.
    long buildCardinality = Math.max(0, getChild(1).getCardinality());
    ProcessingCost buildProcessingCost = ProcessingCost.basicCost(
        getDisplayLabel() + " Build side", buildCardinality, eqJoinPredicateEvalCost);
    return Pair.create(probeProcessingCost, buildProcessingCost);
  }
}
