"""GitHub/GitLab/Codeberg list_repos: topic search and a broader/untagged search must
each get their own budget. The `flatpak` topic is opt-in and most flatpak-shipping
repos never add it, so if topic search alone reaches the shared budget first, the
query built to catch what topic tags miss must still run rather than being skipped."""

from flatsonar_server.crawler.codeberg import CodebergSource, GiteaForge
from flatsonar_server.crawler.forge import RepoInfo
from flatsonar_server.crawler.github import GitHubForge
from flatsonar_server.crawler.gitlab import GitLabForge


async def test_github_code_search_runs_even_after_topic_search_fills_the_budget(monkeypatch):
    forge = GitHubForge(token="tok")
    topic_queries_used = []
    code_queries_used = []

    async def fake_search_repos(ctx, query, budget):
        topic_queries_used.append(query)
        for i in range(budget + 1):  # more on offer than the budget allows
            yield {"full_name": f"topic/{query}/{i}", "html_url": "https://github.com/topic/x",
                   "owner": {"login": "topic"}}

    async def fake_search_code(ctx, query, budget):
        code_queries_used.append(query)
        for i in range(budget + 1):
            yield f"code/{query}/{i}"

    async def fake_get_repo(ctx, full_name):
        return RepoInfo(forge="github", full_name=full_name, html_url=f"https://github.com/{full_name}",
                         owner="code", default_branch="main")

    monkeypatch.setattr(forge, "_search_repos", fake_search_repos)
    monkeypatch.setattr(forge, "_search_code", fake_search_code)
    monkeypatch.setattr(forge, "_get_repo", fake_get_repo)

    repos = [r async for r in forge.list_repos(None, limit=2)]
    assert len(topic_queries_used) == 1  # first topic query alone already filled the budget...
    assert len(code_queries_used) == 1  # ...but code search still ran instead of being skipped
    assert len(repos) == 4  # 2 from topic search + 2 more from code search


async def test_gitlab_untagged_search_runs_even_after_topic_search_fills_the_budget():
    forge = GitLabForge()

    def project(owner, n):
        return {"path_with_namespace": f"{owner}/repo{n}", "web_url": f"https://gitlab.com/{owner}/repo{n}",
                "visibility": "public"}

    class FakeCtx:
        def __init__(self):
            self.urls: list[str] = []

        async def fetch_json(self, url, headers=None):
            self.urls.append(url)
            if "page=1" not in url:
                return None
            if "topic=flatpak" in url:
                return [project("topicowner", 1), project("topicowner", 2), project("topicowner", 3)]
            if "search=flatpak" in url:
                return [project("searchowner", 1), project("searchowner", 2)]
            return None

    ctx = FakeCtx()
    repos = [r async for r in forge.list_repos(ctx, limit=2)]
    assert [r.full_name for r in repos] == [
        "topicowner/repo1", "topicowner/repo2", "searchowner/repo1", "searchowner/repo2",
    ]
    assert any("search=flatpak" in u for u in ctx.urls)  # the untagged search actually ran


async def test_codeberg_untagged_search_runs_even_after_topic_search_fills_the_budget():
    forge = GiteaForge()

    def repo(name):
        return {"full_name": name, "html_url": f"https://codeberg.org/{name}"}

    class FakeCtx:
        def __init__(self):
            self.urls: list[str] = []

        async def fetch_json(self, url, headers=None):
            self.urls.append(url)
            if "page=1" not in url:
                return None
            if "topic=true" in url:
                return {"data": [repo("a/one"), repo("a/two"), repo("a/three")]}
            if "topic=false" in url:
                return {"data": [repo("b/one"), repo("b/two")]}
            return None

    ctx = FakeCtx()
    repos = [r async for r in forge.list_repos(ctx, limit=2)]
    assert [r.full_name for r in repos] == ["a/one", "a/two", "b/one", "b/two"]
    assert any("topic=false" in u for u in ctx.urls)  # the untagged search actually ran


async def test_codeberg_source_builds_one_forge_per_base_url():
    src = CodebergSource(token="tok", base_urls=("https://codeberg.org", "https://example-forgejo.invalid"))
    assert [f.base for f in src.forges] == ["https://codeberg.org", "https://example-forgejo.invalid"]
    # Token only goes to the primary instance - never to a third-party one.
    assert src.forges[0].token == "tok"
    assert src.forges[1].token is None


async def test_codeberg_source_defaults_to_codeberg_org_only():
    src = CodebergSource()
    assert [f.base for f in src.forges] == ["https://codeberg.org"]
