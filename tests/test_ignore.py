from huatuo.git_repo import is_ignored
from huatuo.settings import ReviewConfig


def cfg(**kw):
    return ReviewConfig(ignore=["*.lock"], project_ignore={"g/a": ["deploy/*", "*/deploy/*"]}, **kw)


def test_project_ignore_only_applies_to_that_project():
    c = cfg()
    a = c.ignore_for("g/a")
    assert is_ignored("deploy/k8s/prod.yaml", a) and is_ignored("app/x/deploy/run.sh", a) and is_ignored("go.lock", a)
    assert not is_ignored("internal/deploy_service.go", a)
    assert not is_ignored("deploy/k8s/prod.yaml", c.ignore_for("g/b"))


def test_repo_ignore_can_be_disabled():
    assert "docs/*" in cfg().ignore_for("g/b", ["docs/*"])
    c = cfg(allow_repo_ignore=False)
    assert "docs/*" not in c.ignore_for("g/a", ["docs/*"])
    assert "deploy/*" in c.ignore_for("g/a", ["docs/*"])
