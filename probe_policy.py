# SPDX-License-Identifier: AGPL-3.0-only
import pytest
import test_independent as p
with pytest.MonkeyPatch.context() as mp:
    mp.setattr(p.M, '_download_host_resolve_enabled', lambda: False)
    p.test_latest_pointer_must_not_hide_newer_publication(mp)
print('PASS: API publication-order lookup selects release-2 in the identical scenario; production files unchanged.')
