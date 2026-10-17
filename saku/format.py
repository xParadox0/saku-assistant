"""Display helpers shared by the CLI and the dashboard."""
from decimal import Decimal, ROUND_HALF_UP


def rupiah(v) -> str:
    v = Decimal(v or 0)
    sign = "-" if v < 0 else ""
    return f"{sign}Rp{abs(int(v)):,}".replace(",", ".")


def signed_rupiah(v) -> str:
    """'+Rp1.000' / '-Rp1.000' / 'Rp0', for changes and transaction amounts."""
    v = Decimal(v or 0)
    return ("+" if v > 0 else "") + rupiah(v)


def rupiah_short(v) -> str:
    """Compact label for charts: 19,5 jt / 850 rb. Indonesian units, decimal comma."""
    v = abs(Decimal(v or 0))
    if v >= 1_000_000_000:
        n, unit = v / 1_000_000_000, "M"
    elif v >= 1_000_000:
        n, unit = v / 1_000_000, "jt"
    elif v >= 1_000:
        n, unit = v / 1_000, "rb"
    else:
        return str(int(v))
    n = n.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = f"{n:f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} {unit}"
