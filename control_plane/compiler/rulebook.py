import re
from typing import Any, Dict, List


class RulebookCompiler:
    """Parses executable Markdown rulebooks into structured transformation objects[cite: 93]."""

    @staticmethod
    def parse_markdown(content: str) -> Dict[str, Any]:
        field_rules = []
        crosswalks = []
        joins = []
        validations = []

        if not content:
            return {
                "fieldRules": field_rules,
                "crosswalks": crosswalks,
                "joins": joins,
                "validationRules": validations
            }

        sections = re.split(r"^##\s+", content, flags=re.MULTILINE)

        for section in sections:
            lines = [l.strip() for l in section.split("\n") if l.strip()]
            if not lines:
                continue

            header = lines[0].lower()

            if "field transformation" in header:
                for line in lines[1:]:
                    if not line.startswith("|") or "Target field" in line or "---" in line:
                        continue
                    line_clean = line.replace("\\|", "___PIPE___")
                    cols = [c.strip().replace("___PIPE___", "||") for c in line_clean.split("|")[1:-1]]
                    if len(cols) >= 4:
                        raw_expr = cols[3]

                        vars_found = re.findall(r"\{\{\s*([^\}]+)\s*\}\}", raw_expr)
                        field_lineage = []

                        for v in vars_found:
                            v_clean = v.strip()
                            if "." in v_clean and (v_clean.endswith(".csv") or v_clean.endswith(".psv") or ".csv." in v_clean or ".psv." in v_clean):
                                parts = v_clean.rsplit(".", 1)
                                field_lineage.append({"column": parts[1], "file": parts[0]})
                            else:
                                field_lineage.append({"column": v_clean, "file": None})

                        field_rules.append({
                            "entity": cols[0],
                            "targetField": cols[1],
                            "sourceFile": cols[2],
                            "expression": raw_expr,
                            "fieldLineage": field_lineage
                        })

            elif "value crosswalk" in header:
                for line in lines[1:]:
                    if not line.startswith("|") or "Target field" in line or "---" in line:
                        continue
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    if len(cols) >= 4:
                        crosswalks.append({
                            "sourceFile": cols[0],
                            "targetField": cols[1],
                            "sourceValue": cols[2],
                            "targetValue": cols[3]
                        })

            elif "entity join" in header:
                for line in lines[1:]:
                    if not line.startswith("|") or "Left field" in line or "---" in line:
                        continue
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    if len(cols) >= 5:
                        joins.append({
                            "leftFile": cols[0],
                            "leftField": cols[1],
                            "rightFile": cols[2],
                            "rightField": cols[3],
                            "joinType": cols[4]
                        })

            elif "validation rule" in header:
                for line in lines[1:]:
                    if not line.startswith("|") or "Check type" in line or "---" in line:
                        continue
                    cols = [c.strip() for c in line.split("|")[1:-1]]
                    if len(cols) >= 5:
                        validations.append({
                            "entity": cols[0],
                            "sourceFile": cols[1],
                            "checkType": cols[2],
                            "scope": cols[3],
                            "ruleText": cols[4]
                        })

        return {
            "fieldRules": field_rules,
            "crosswalks": crosswalks,
            "joins": joins,
            "validationRules": validations
        }