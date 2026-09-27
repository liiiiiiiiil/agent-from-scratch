def calculate_discount_cents(subtotal_cents, discount_percent):
    if subtotal_cents < 0 or not 0 <= discount_percent <= 100:
        raise ValueError("invalid order discount")
    return subtotal_cents * discount_percent // 100


def amount_due_cents(subtotal_cents, discount_percent):
    return subtotal_cents - calculate_discount_cents(subtotal_cents, discount_percent)
