def render_receipt(subtotal_cents, discount_percent):
    dollars = subtotal_cents / 100
    return f"Subtotal: ${dollars:.2f}\nAmount due: ${dollars:.2f}\n"
