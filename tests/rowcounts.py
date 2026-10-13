"""``python -m tests.rowcounts``: rows each ported report yields per recording, against the recording's."""

from fcli.reports import REGISTRY

from .replay import node_state, recorded, recordings
from .test_release_matrix import PARAMS, PORTED

for path in recordings():
    cells = []
    for new, old in PORTED.items():
        rec = recorded(path, old)
        got = len(REGISTRY[new].run(node_state(path, old), **PARAMS.get(new, {})))
        cells.append(f"{new}={got}/{len(rec['rows']) if rec else '-'}")
    print(f"{path.parent.name:8} {path.name[:28]:28}", " ".join(cells))
