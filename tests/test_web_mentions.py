from wecom_reader import web


def test_index_contains_escaped_single_pass_mention_highlighting() -> None:
    response = web.app.test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ".mention {" in html
    assert "function highlightMentions(content, mentions)" in html
    assert "names.map(escapeRegExp).join('|')" in html
    assert "html += escapeHtml(text.slice(offset, match.index))" in html
    assert 'class="mention"' in html
    assert "highlightMentions(content, m.mentions)" in html
