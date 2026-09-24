from service import store
from service.checkout import Checkout
from service.tests.conftest import LocalBox


def open_local(commit):
    return Checkout(LocalBox(), commit)


def test_checkout_has_every_ref_and_no_secrets(agent_repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    base = store._git("rev-parse", "main").strip()
    store.pin_ref("main", "refs/jobs/x/0")
    work = open_local(base)

    assert work.head() == base
    assert "refs/jobs/x/0" in work.bash("git for-each-ref")
    out = work.bash("pwd; env")
    assert work.path in out and "secret" not in out and "SERVICE_DATABASE_URL" not in out
    assert out.endswith("[exit code 0]")


def test_bash_output_is_storable(agent_repo):
    work = open_local("main")
    assert work.bash(r"printf 'a\377\000b'") == "a�b[exit code 0]"


def test_commit_comes_back_into_the_agent_repo(agent_repo):
    base = store._git("rev-parse", "main").strip()
    work = open_local(base)
    work.bash("printf 'x\\0y' > data.bin && rm my_agent/core.py && mkdir -p my_agent/__pycache__ && touch my_agent/__pycache__/c.pyc")

    commit, parent = work.commit("rewrite", "refs/jobs/x/1")
    assert parent == base
    assert store._git("rev-parse", "refs/jobs/x/1").strip() == commit
    assert store._git("ls-tree", "-r", "--name-only", commit).split() == ["data.bin"]
    assert store.commit_message(commit) == "rewrite"


def test_commit_on_the_meta_agents_own_commit(agent_repo):
    base = store._git("rev-parse", "main").strip()
    work = open_local(base)
    work.bash("echo a >> my_agent/core.py && git commit -qam own && echo b >> my_agent/core.py")

    commit, parent = work.commit("more", "refs/jobs/x/1")
    assert store._git("rev-parse", f"{parent}^").strip() == base  # the meta-agent's commit came along
    assert store._git("show", f"{commit}:my_agent/core.py").endswith("a\nb\n")


def test_no_change_is_none(agent_repo):
    work = open_local("main")
    work.bash("mkdir -p my_agent/__pycache__ && touch my_agent/__pycache__/c.pyc")
    assert work.commit("nothing", "refs/jobs/x/1") is None
