import re

def extract_note_links(content: str) -> list[str]:
    return re.findall(r'\[\[([^\]]+)\]\]', content)

def extract_links(content: str) -> list[str]:
    # Find all [[Note Title]] style links
    return re.findall(r"\[\[([^\]]+)\]\]", content)