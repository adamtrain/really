from really.export import filename, markdown, slug, to_dict, write_markdown
from really.extract import Article

ESSAY = Article(
    title='Notes: "Café" & Other Queues',
    author="Sam Okafor",
    site="Fieldnotes",
    published="2025-11-02",
    content="First paragraph.\n\nSecond, with a [link](https://example.com).",
    text="First paragraph. Second, with a link.",
)


def archived(store):
    item = store.add("https://fieldnotes.example/cafe", ESSAY, fetched=True, tags=["habits", "ml"])
    store.edit(item.id, note="Read this again in spring.\nIt holds up.")
    return store.archive(item.id)


def test_slug():
    assert slug('Notes: "Café" & Other Queues') == "notes-cafe-other-queues"
    assert slug("???") == "untitled"
    assert len(slug("word " * 40)) <= 60


def test_markdown_has_front_matter_note_and_content(store):
    item = archived(store)
    document = markdown(item, store.content(item.id))
    front, body = document.removeprefix("---\n").split("\n---\n", 1)
    assert 'title: "Notes: \\"Café\\" & Other Queues"' in front
    assert 'author: "Sam Okafor"' in front
    assert 'url: "https://fieldnotes.example/cafe"' in front
    assert 'published: "2025-11-02"' in front
    assert 'tags: ["habits", "ml"]' in front
    assert body.startswith('\n# Notes: "Café" & Other Queues\n\n> Read this again in spring.\n> It')
    assert body.endswith("Second, with a [link](https://example.com).\n")


def test_write_markdown_is_repeatable(store, tmp_path):
    item = archived(store)
    entries = [(item, store.content(item.id))]
    target = tmp_path / "notes" / "reading"
    first = write_markdown(target, entries)
    second = write_markdown(target, entries)
    assert first == second == [target / f"notes-cafe-other-queues-{item.id}.md"]
    assert filename(item) == first[0].name
    assert len(list(target.iterdir())) == 1


def test_to_dict(store):
    item = archived(store)
    data = to_dict(item)
    assert data["title"] == item.title
    assert data["tags"] == ["habits", "ml"]
    assert data["state"] == "archived"
    assert data["minutes"] == 1
    assert data["archived_at"].startswith("20")
    assert "content" not in data
    assert to_dict(item, "the copy")["content"] == "the copy"
