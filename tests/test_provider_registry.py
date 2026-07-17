"""Static integrity checks for the imagery provider registry.

Everything here is offline: the provider ``.lay`` files, the extent
``.ext`` files, and the combined-provider ``.comb`` files must parse and
cross-reference cleanly without any network access. This guards against
the failure mode found in July 2026 where the EUR combined provider
referenced layers whose definitions were broken or whose projections
could not be resolved.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="module")
def imagery(monkeypatch_module=None):
    """Import O4_Imagery_Utils with the repo root as data root and all
    registries initialized."""
    old_cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    for path in (os.path.join(REPO_ROOT, "src"),
                 os.path.join(REPO_ROOT, "Providers")):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import O4_Imagery_Utils as IMG

        if not IMG.providers_dict:
            IMG.initialize_extents_dict()
            IMG.initialize_color_filters_dict()
            IMG.initialize_providers_dict()
            IMG.initialize_combined_providers_dict()
        yield IMG
    finally:
        os.chdir(old_cwd)


def test_every_lay_file_registered(imagery):
    """Each .lay file must survive parsing and land in providers_dict
    (a parse failure only prints, so compare file names to keys)."""
    expected = set()
    providers_root = os.path.join(REPO_ROOT, "Providers")
    for dir_name in os.listdir(providers_root):
        subdir = os.path.join(providers_root, dir_name)
        if not os.path.isdir(subdir):
            continue
        for file_name in os.listdir(subdir):
            if file_name.endswith(".lay"):
                expected.add(file_name[:-len(".lay")])
    missing = expected - set(imagery.providers_dict)
    assert not missing, (
        "Provider definition files failed to parse: %s" % sorted(missing))


def test_eur_combined_provider_complete(imagery):
    """EUR must exist, keep every non-comment line it declares, and end
    in a global fallback layer so any European tile can build."""
    assert "EUR" in imagery.combined_providers_dict
    comb = imagery.combined_providers_dict["EUR"]
    declared = 0
    with open(os.path.join(REPO_ROOT, "Providers", "EUR.comb")) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                declared += 1
    assert len(comb) == declared, (
        "EUR.comb declares %d layers but only %d parsed — the loader "
        "drops lines with unknown providers/extents/priorities"
        % (declared, len(comb)))
    assert comb[-1]["extent_code"] == "global", (
        "EUR must end with a global fallback layer")
    assert comb[-1]["priority"] == "low"


def test_combined_layers_resolve(imagery):
    """Every layer of every .comb: provider exists, extent exists, and
    the provider's projection resolves (guards the ESRI-code KeyError
    that crashed Slovenia downloads)."""
    import O4_Geo_Utils as GEO

    for provider_code, comb in imagery.combined_providers_dict.items():
        for rlayer in comb:
            layer_code = rlayer["layer_code"]
            assert layer_code in imagery.providers_dict, (
                provider_code, layer_code)
            extent_code = rlayer["extent_code"]
            lookup = extent_code[1:] if extent_code.startswith("!") \
                else extent_code
            assert lookup in imagery.extents_dict, (provider_code, lookup)
            provider = imagery.providers_dict[layer_code]
            if "epsg_code" in provider:
                code = int(provider["epsg_code"])
                assert code in GEO.epsg, (
                    "Projection %s of provider %s is unresolved"
                    % (code, layer_code))
            if lookup != "global":
                mask_path = imagery.extent_mask_image_path(lookup)
                assert os.path.isfile(mask_path), (
                    "Missing extent mask image for %s: %s"
                    % (lookup, mask_path))


def test_no_dead_providers_resurrected(imagery):
    """The providers removed in the July 2026 endpoint audit must stay
    gone: their services are retired and their definitions broken."""
    dead = {"DK", "NIB", "CH_ZH", "CH_VS", "GeoPunt2012", "GeoPunt2015",
            "CRO_2017", "SLO_2016", "SLO_2009_2011", "DOP40", "IAU2008",
            "LittoV2", "Wallonie2013"}
    resurrected = dead & set(imagery.providers_dict)
    assert not resurrected, sorted(resurrected)


def test_custom_url_module_importable(imagery):
    """O4_Custom_URL must import and only reference live providers."""
    assert imagery.has_URL, "Providers/O4_Custom_URL.py failed to import"
    import O4_Custom_URL as URL

    stale = set(URL.custom_url_list) & {"DK", "DOP40", "NIB"}
    assert not stale, sorted(stale)
