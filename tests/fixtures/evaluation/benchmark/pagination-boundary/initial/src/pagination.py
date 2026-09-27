def paginate(items, page_size):
    if page_size < 1:
        raise ValueError("page_size must be positive")
    pages = []
    for offset in range(0, len(items) - page_size, page_size):
        pages.append(items[offset:offset + page_size])
    return pages
