from desk.collectors.filing_text import filing_folder, html_to_text, pick_documents


def test_html_to_text_drops_scripts_and_keeps_blocks() -> None:
    page = (
        "<html><head><title>x</title><style>p{}</style></head><body>"
        "<p>AEP raised its capital plan</p><script>var a=1;</script>"
        "<div>to&nbsp;$54 billion.</div></body></html>"
    )
    assert html_to_text(page) == "AEP raised its capital plan\nto $54 billion."


def test_filing_folder_from_index_or_document() -> None:
    base = "https://www.sec.gov/Archives/edgar/data/4904/000000490426000123"
    assert filing_folder(f"{base}/0000004904-26-000123-index.htm") == base
    assert filing_folder(f"{base}/aep-8k.htm") == base


def test_pick_documents_main_plus_exhibit_99() -> None:
    index = {
        "directory": {
            "item": [
                {"name": "0000004904-26-000123-index.htm"},
                {"name": "aep-8k.htm"},
                {"name": "aep-ex991.htm"},
                {"name": "aep-ex101.htm"},
                {"name": "R1.xml"},
            ]
        }
    }
    assert pick_documents(index, "aep-8k.htm") == ["aep-8k.htm", "aep-ex991.htm"]
    assert pick_documents(index, None) == ["aep-8k.htm", "aep-ex991.htm"]
