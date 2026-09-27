from pricing import amount_due_cents, calculate_discount_cents


def _money(cents):
    return f"${cents // 100}.{cents % 100:02d}"


def render_receipt(subtotal_cents, discount_percent):
    discount = calculate_discount_cents(subtotal_cents, discount_percent)
    due = amount_due_cents(subtotal_cents, discount_percent)
    return (
        f"Subtotal: {_money(subtotal_cents)}\n"
        f"Discount ({discount_percent}%): -{_money(discount)}\n"
        f"Amount due: {_money(due)}\n"
    )
