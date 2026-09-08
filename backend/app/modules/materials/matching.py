def filename_matches(file_name: str, title: str) -> bool:
    needle = title.strip().casefold()
    return bool(needle) and needle in file_name.casefold()
