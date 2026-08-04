from queries_1c import load_query_catalog


def test_catalog_exposes_complete_downloadable_outbound_query():
    catalog = load_query_catalog()

    assert len(catalog) == 1
    query = catalog[0]
    assert query.slug == "mass_outbound_orders"
    assert query.text.startswith("ВЫБРАТЬ")
    assert "Документ.РасходныйОрдерНаТовары" in query.text
    assert all(f"&{name}" in query.text for name, _description in query.parameters)
    assert all(name in query.text for name, _description in query.result_columns)


def test_query_file_has_no_template_placeholders():
    query = load_query_catalog()[0]

    assert "TODO" not in query.text
    assert "..." not in query.text
    assert query.text.endswith("\n")
