"""The archive: every recording Curie makes, normalised into episodes, in
one SQLite file (knowledge plan, section 4). Nothing here is read on a
user's turn; the consolidation pipelines read it in the background."""
from curie.archive.store import Archive  # noqa: F401
from curie.archive.episodes import build_session  # noqa: F401
