"""Bedragen en koersen in Nederlandse notatie."""


def _nl(s):
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def eur(x):
    """Geldbedrag: € 1.234,56"""
    return f"€ {_nl(f'{x:,.2f}')}"


def px(x):
    """Koers met genoeg decimalen, ook voor goedkope munten: € 0,056204"""
    a = abs(x)
    d = 2 if a >= 100 else 4 if a >= 1 else 6 if a >= 0.01 else 8
    return f"€ {_nl(f'{x:,.{d}f}')}"
