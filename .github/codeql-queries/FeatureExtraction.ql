/**
 * @name Extract Features for Cohesion Analysis (v3 - Corrected)
 * @description This query extracts all referenced functions, variables and types for each source file.
 * @kind table
 */
import cpp

from File f, Locatable ref, string featureType
where
  f.getRelativePath().matches("be/src/%") and
  (
    exists(Call c | c.getLocation().getFile() = f and ref = c.getTarget() and featureType = "FunctionCall") or
    exists(VariableAccess va | va.getLocation().getFile() = f and ref = va.getTarget() and featureType = "VariableAccess") or
    exists(TypeAccess ta | ta.getLocation().getFile() = f and ref = ta.getType() and featureType = "TypeAccess")
  ) and
  ref.toString() != ""
select f.getRelativePath() as file, featureType, ref.toString() as feature
