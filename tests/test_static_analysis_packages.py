from huatuo.static_analysis import _packages


class FakeMirror:
    def __init__(self, files: dict[str, str]):
        self.files = files

    def ls_tree(self, sha, path=""):
        pre = f"{path}/" if path else ""
        return sorted({k[len(pre):].split("/")[0] for k in self.files if k.startswith(pre)})

    def show(self, sha, path):
        return self.files.get(path)


def test_skips_dir_whose_files_all_need_custom_tag():
    m = FakeMirror({
        "integration/a_test.go": "//go:build integration\n\npackage x\n",
        "integration/helpers_test.go": "//go:build integration && !race\n\npackage x\n",
        "svc/a.go": "package svc\n",
    })
    assert _packages(m, "sha", {"integration", "svc", "missing"}) == ["./svc/"]


def test_keeps_dir_with_any_untagged_or_platform_only_file():
    m = FakeMirror({
        "mixed/a.go": "package m\n",
        "mixed/a_integration_test.go": "//go:build integration\n\npackage m\n",
        "plat/a_linux.go": "//go:build linux && amd64\n\npackage p\n",
    })
    assert _packages(m, "sha", {"mixed", "plat"}) == ["./mixed/", "./plat/"]
