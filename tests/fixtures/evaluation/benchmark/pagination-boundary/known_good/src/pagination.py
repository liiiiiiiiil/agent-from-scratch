def paginate(items, page_size):
    if page_size < 1:
        raise ValueError("page_size must be positive")
    return [items[offset:offset + page_size] for offset in range(0, len(items), page_size)]
