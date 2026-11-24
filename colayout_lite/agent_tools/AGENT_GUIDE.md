# CoLayout API maintenance

The live tools discovery and contract.py define supported arguments. See docs/en/api.md in the source repository and docs/API.md in the generated layout-agent folder.

All edits share the human Editor. Cooperative `workspace` actions (`create_region`, `dissolve_region`, `sync_regions`, `pull`, `sync`), `release_region(sync_to_virtuoso=True)`, and on-demand `inspect_region(kind="parasitics")` invoke the same shared Editor, Adapter and domain services as the UI. Use scoped claims, preserve request identity after timeout, and require current local and server evidence for done. Do not add direct Bridge, arbitrary code execution or process approval to the layout client.
