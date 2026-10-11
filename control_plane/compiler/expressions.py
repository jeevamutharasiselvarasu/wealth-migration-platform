import re
from datetime import datetime
from typing import Any, Dict, Optional


class ExpressionEvaluator:
    """Evaluates approved transformation expressions safely without eval()[cite: 95]."""

    VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_\.]+)\s*\}\}")

    @classmethod
    def evaluate(cls, expression: str, record: Dict[str, Any]) -> Any:
        """
        Evaluates a transformation expression against a source record dictionary[cite: 95].
        Supports:
        - Direct variable substitutions: {{field}}
        - PERIOD_START({{period_end}})
        - TOKENIZE({{sensitive_field}})
        - CONCAT({{left}}, {{right}})
        """
        if not expression:
            return None

        expr = expression.strip()

        # Handle PERIOD_START(date_expr)
        period_start_match = re.match(r"^PERIOD_START\((.+)\)$", expr, re.IGNORECASE)
        if period_start_match:
            inner_val = cls.evaluate(period_start_match.group(1), record)
            return cls._derive_period_start(inner_val)

        # Handle TOKENIZE(val_expr)
        tokenize_match = re.match(r"^TOKENIZE\((.+)\)$", expr, re.IGNORECASE)
        if tokenize_match:
            inner_val = cls.evaluate(tokenize_match.group(1), record)
            return cls._tokenize(inner_val)

        # Handle CONCAT(expr1, expr2)
        concat_match = re.match(r"^CONCAT\((.+),\s*(.+)\)$", expr, re.IGNORECASE)
        if concat_match:
            val1 = cls.evaluate(concat_match.group(1), record)
            val2 = cls.evaluate(concat_match.group(2), record)
            return f"{val1 or ''}{val2 or ''}"

        # Direct variable substitution: {{field}}
        var_match = cls.VAR_PATTERN.fullmatch(expr)
        if var_match:
            field_name = var_match.group(1)
            if "." in field_name:
                field_name = field_name.rsplit(".", 1)[1]
            return record.get(field_name)

        # Mixed string interpolation: e.g. "{{first_name}} {{last_name}}"
        if cls.VAR_PATTERN.search(expr):
            def replacer(m):
                fname = m.group(1)
                if "." in fname:
                    fname = fname.rsplit(".", 1)[1]
                val = record.get(fname)
                return str(val) if val is not None else ""
            return cls.VAR_PATTERN.sub(replacer, expr)

        return expr

    @staticmethod
    def _derive_period_start(date_val: Any) -> Optional[str]:
        """Derives the first day of the month for a given period end date[cite: 95]."""
        if not date_val:
            return None
        date_str = str(date_val).strip()

        formats = ["%Y-%m-%d", "%m/%Y", "%m/%d/%Y", "%Y/%m/%d"]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-01")
            except ValueError:
                continue
        return date_str

    @staticmethod
    def _tokenize(val: Any) -> Optional[str]:
        """Tokenizes sensitive PII fields (e.g. Tax ID / SSN)[cite: 95]."""
        if not val:
            return None
        clean_val = str(val).replace("-", "").strip()
        if len(clean_val) >= 4:
            return f"TOK-xxx-xx-{clean_val[-4:]}"
        return f"TOK-{clean_val}"