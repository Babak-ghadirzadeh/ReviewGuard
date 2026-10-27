# Contributing

ReviewGuard welcomes source adapters, scientific validation and usability improvements. Start with the architecture and validation documents. Keep local deployment and free/open-source operation working.

Create a virtual environment, install `-e '.[pdf]'`, and run `python -m unittest discover -s tests -v`. Add meaningful fixtures when changing contribution identification, identity resolution, notice handling or editorial decisions. Synthetic or appropriately licensed fixtures only: do not commit publisher PDFs, private manuscripts, personal records, credentials or model weights.

A new extraction adapter should retain evidence text, source location, document hash, method version and explicit abstention reasons. Test false positive cases such as background citations, reporting guidelines, exclusions, previous reviews, repeated study rows, conflicting reference labels and missing identifiers. Generic AI confidence is not enough to promote a link.

Report software correctness and scientific accuracy separately. Describe the concrete trigger, changed behavior, evidence and relevant checks in a pull request. Acknowledge coverage limits and upstream terms. Preserve existing code provenance.
