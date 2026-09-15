"""Decide whether an SPDX license expression counts as open source.

Flatsonar lists only apps whose license is on the OSI-approved or FSF-free
list. Expressions (``GPL-3.0-or-later AND MIT``, ``(X OR Y)``) are accepted
when every identifier in them is open source; ``LicenseRef-*`` (custom /
proprietary) is never accepted.
"""

from __future__ import annotations

import re

# A pragmatic allow-list of SPDX identifiers that are OSI-approved and/or FSF-free.
# Deprecated forms (GPL-3.0+, GPL-3.0) are kept because AppStream data still uses them.
_OPEN_SOURCE = {
    # permissive
    "MIT", "MIT-0", "X11", "ISC", "BSD-1-Clause", "BSD-2-Clause", "BSD-2-Clause-Patent",
    "BSD-3-Clause", "BSD-3-Clause-Clear", "BSD-4-Clause", "0BSD", "Apache-1.1", "Apache-2.0",
    "Zlib", "zlib-acknowledgement", "BSL-1.0", "Unlicense", "CC0-1.0", "WTFPL", "PostgreSQL",
    "NCSA", "Python-2.0", "Python-2.0.1", "PSF-2.0", "Artistic-2.0", "Artistic-1.0-Perl",
    "curl", "OFL-1.1", "OFL-1.1-RFN", "OFL-1.1-no-RFN", "OFL-1.0", "Ruby", "PHP-3.01",
    "Vim", "TCL", "NTP", "HPND", "ICU", "Xnet", "UPL-1.0", "MulanPSL-2.0", "BlueOak-1.0.0",
    "Fair", "Beerware", "SMLNJ", "MIT-CMU", "MIT-feh", "MIT-Modern-Variant", "MIT-open-group",
    "Libpng", "libpng-2.0", "IJG", "OpenSSL", "SSLeay", "NetCDF", "Plexus", "W3C", "W3C-20150513",
    "ZPL-2.0", "ZPL-2.1", "EFL-2.0", "ClArtistic", "AFL-3.0", "AFL-2.1", "AFL-2.0",
    # weak copyleft
    "LGPL-2.0", "LGPL-2.0+", "LGPL-2.0-only", "LGPL-2.0-or-later",
    "LGPL-2.1", "LGPL-2.1+", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0", "LGPL-3.0+", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "MPL-1.0", "MPL-1.1", "MPL-2.0", "MPL-2.0-no-copyleft-exception",
    "EPL-1.0", "EPL-2.0", "CDDL-1.0", "CDDL-1.1", "CPL-1.0", "OSL-3.0", "OSL-2.1", "OSL-2.0",
    "EUPL-1.1", "EUPL-1.2", "CECILL-2.1", "CECILL-B", "CECILL-C", "LiLiQ-P-1.1", "LiLiQ-R-1.1",
    "LiLiQ-Rplus-1.1", "MS-PL", "MS-RL", "Motosoto", "Naumen", "Nokia", "OCLC-2.0",
    "OLDAP-2.8", "OSET-PL-2.1", "QPL-1.0", "RPL-1.1", "RPL-1.5", "RPSL-1.0", "RSCPL",
    "SimPL-2.0", "Sleepycat", "SPL-1.0", "Watcom-1.0", "wxWindows",
    "BSD-3-Clause-LBNL", "APL-1.0", "APSL-2.0", "CATOSL-1.1", "CNRI-Python", "CUA-OPL-1.0",
    "ECL-1.0", "ECL-2.0", "Entessa", "Frameworx-1.0", "IPA", "IPL-1.0", "Intel", "LPL-1.0",
    "LPL-1.02", "LPPL-1.3c", "MirOS", "Multics", "NASA-1.3", "NGPL", "NPOSL-3.0", "OGTSL",
    "Zimbra-1.3", "Zend-2.0",
    # strong copyleft
    "GPL-1.0", "GPL-1.0+", "GPL-1.0-only", "GPL-1.0-or-later",
    "GPL-2.0", "GPL-2.0+", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0+", "GPL-3.0-only", "GPL-3.0-or-later",
    "AGPL-1.0", "AGPL-1.0-only", "AGPL-1.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "Artistic-1.0", "Artistic-1.0-cl8", "GFDL-1.1", "GFDL-1.2", "GFDL-1.3",
    "GFDL-1.1-only", "GFDL-1.1-or-later", "GFDL-1.2-only", "GFDL-1.2-or-later",
    "GFDL-1.3-only", "GFDL-1.3-or-later", "GFDL-1.3-no-invariants-or-later",
    "CC-BY-4.0", "CC-BY-3.0", "CC-BY-SA-4.0", "CC-BY-SA-3.0", "ODbL-1.0", "OGL-UK-3.0",
    "Unicode-DFS-2016", "Unicode-3.0", "BitTorrent-1.1", "Condor-1.1", "FSFAP", "FTL",
    "ImageMagick", "Imlib2", "OpenLDAP", "SGI-B-2.0", "xinetd", "YPL-1.1", "SISSL",
}

_TOKEN = re.compile(r"\(|\)|\bAND\b|\bOR\b|\bWITH\b|[A-Za-z0-9.+\-]+", re.IGNORECASE)


def normalise(expr: str | None) -> str:
    return (expr or "").strip()


def is_open_source(expr: str | None) -> bool:
    """True if every license identifier in the SPDX expression is on the allow-list.

    Empty / None -> False (unknown is not open source).
    """
    expr = normalise(expr)
    if not expr:
        return False
    tokens = _TOKEN.findall(expr)
    if not tokens:
        return False

    expect_exception = False
    saw_license = False
    for tok in tokens:
        up = tok.upper()
        if tok in ("(", ")") or up in ("AND", "OR"):
            continue
        if up == "WITH":
            expect_exception = True
            continue
        if expect_exception:
            expect_exception = False  # exception identifier, e.g. GCC-exception-3.1
            continue
        saw_license = True
        if tok.startswith(("LicenseRef-", "DocumentRef-")):
            return False
        if tok not in _OPEN_SOURCE and tok.rstrip("+") not in _OPEN_SOURCE:
            return False
    return saw_license
