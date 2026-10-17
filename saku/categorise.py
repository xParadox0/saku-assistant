"""Built-in categories. Deterministic keyword rules, no model, so every label can be explained.

Order of precedence when labelling a transaction (see Repo.apply_categories):
1. the transaction kind: own transfers, fees and refunds have fixed categories;
2. positive normal amounts are income;
3. the tenant's own category_rule rows;
4. the built-in patterns below (PostgreSQL case-insensitive regex);
5. otherwise 'other'.
"""
from __future__ import annotations

# key: (label, icon id in the sprite, colour token)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "food":          ("Food & drink",   "i-food",     "c-food"),
    "groceries":     ("Groceries",      "i-basket",   "c-groceries"),
    "transport":     ("Transport",      "i-ride",     "c-transport"),
    "bills":         ("Bills",          "i-bolt",     "c-bills"),
    "subscriptions": ("Subscriptions",  "i-repeat",   "c-subs"),
    "shopping":      ("Shopping",       "i-bag",      "c-shopping"),
    "health":        ("Health",         "i-heart",    "c-health"),
    "fees":          ("Bank fees",      "i-receipt",  "c-fees"),
    "other":         ("Other",          "i-dots",     "c-other"),
    "income":        ("Income",         "i-arrow-in", "c-income"),
    "transfer":      ("Own transfer",   "i-swap",     "c-transfer"),
    "refund":        ("Refund",         "i-undo",     "c-income"),
}

# (category, regex). PostgreSQL ARE syntax: \y is a word boundary (\b would mean backspace).
# Kept short and generic; tenant rules cover anything personal.
BUILTIN_RULES: list[tuple[str, str]] = [
    ("subscriptions", r"(netflix|spotify|youtube|disney|vidio|apple\.com|google ?storage|icloud|streaming|subs(cription)?\y|langganan)"),
    ("bills",         r"(token listrik|\ypln\y|pdam|telkom|indihome|first ?media|biznet|pulsa|paket data|bpjs|tagihan|listrik|internet)"),
    ("transport",     r"(gojek|goride|gocar|grab(bike|car)?|maxim|ojek|\ykrl\y|commuter|mrt|transjakarta|\ytol\y|parkir|pertamina|shell|bensin|\ybbm\y|kai\y|tiket kereta)"),
    ("groceries",     r"(supermarket|indomaret|alfamart|alfamidi|superindo|hypermart|transmart|lottemart|ranch market|grocer|sayur|pasar)"),
    ("food",          r"(gofood|grabfood|shopeefood|warung|resto|restaurant|cafe|kafe|kopi|coffee|bakso|nasi|mie|ayam|sate|martabak|makan|food|starbucks|mcd|kfc)"),
    ("shopping",      r"(tokopedia|shopee(?!pay|food)|lazada|blibli|bukalapak|zalora|uniqlo|toko|store|mall)"),
    ("health",        r"(apotek|apotik|pharmacy|klinik|clinic|rumah sakit|\yrs\y|hospital|dokter|halodoc|alodokter|kimia farma|guardian)"),
]


def label(key: str | None) -> tuple[str, str, str]:
    return CATEGORIES.get(key or "other", CATEGORIES["other"])
