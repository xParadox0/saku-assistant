"""Display helpers shared by the CLI and the dashboard."""
from decimal import Decimal


def rupiah(v) -> str:
    v = Decimal(v or 0)
    sign = "-" if v < 0 else ""
    return f"{sign}Rp{abs(int(v)):,}".replace(",", ".")
