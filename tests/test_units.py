from ai_cr.diff_parser import parse_diff
from ai_cr.git_repo import locate_snippet
from ai_cr.graph.gate import compute_conclusion
from ai_cr.poller import parse_command

DIFF = """diff --git a/svc/a.go b/svc/a.go
index 1111111..2222222 100644
--- a/svc/a.go
+++ b/svc/a.go
@@ -10,6 +10,8 @@ func Run() {
 	x := 1
 	y := 2
-	z := 3
+	z := x + y
+	m[key] = z
+	go work(m)
 	return
 }
 
diff --git a/new.go b/new.go
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.go
@@ -0,0 +1,2 @@
+package main
+func f() {}
"""


def test_parse_diff_line_numbers():
    files = parse_diff(DIFF)
    assert [f.path for f in files] == ["svc/a.go", "new.go"]
    a = files[0]
    assert a.added_lines() == {12, 13, 14}
    assert a.position_for(13) == {"new_line": 13}
    assert a.position_for(10) == {"new_line": 10, "old_line": 10}
    assert a.position_for(99) is None
    assert a.nearest_commentable(16) == 14
    assert "L13 +" in a.annotated() and "     - " in a.annotated()
    assert files[1].new_file and files[1].added_lines() == {1, 2}


def test_locate_snippet_ignores_whitespace_and_picks_nearest():
    content = "a\n  foo(x)\nb\nfoo(x)\nc\n"
    assert locate_snippet(content, "foo(x)", 4) == 4
    assert locate_snippet(content, "  foo(x)  \n b", 1) == 2
    assert locate_snippet(content, "bar()", 1) is None


def f(sev, status):
    return {"severity": sev, "status": status}


def test_gate():
    assert compute_conclusion([]) == "APPROVE"
    assert compute_conclusion([f("P0", "OPEN")]) == "REQUEST_CHANGES"
    assert compute_conclusion([f("P0", "ESCALATED")]) == "REQUEST_CHANGES"
    assert compute_conclusion([f("P0", "WAIVED"), f("P0", "FIXED"), f("P0", "WITHDRAWN")]) == "APPROVE"
    assert compute_conclusion([f("P1", "OPEN")]) == "REQUEST_CHANGES"
    assert compute_conclusion([f("P1", "ESCALATED")]) == "COMMENT"
    assert compute_conclusion([f("P1", "DEFERRED")]) == "APPROVE"
    assert compute_conclusion([f("P2", "OPEN")]) == "COMMENT"
    assert compute_conclusion([f("P0", "NEW")]) == "APPROVE"


def test_parse_command():
    assert parse_command("/ai-confirm 确实有竞态") == ("confirm", "确实有竞态")
    assert parse_command("/ai-downgrade P2") == ("downgrade", "P2")
    assert parse_command("/AI-review") == ("review", "")
    assert parse_command("我觉得 /ai-accept") is None


def test_clean_evidence():
    from ai_cr.git_repo import clean_evidence
    raw = 'L10 +  go func() {\\\\nL11 + \\\\tcache[k] = v\\\\nL12 + }()\\n} '
    assert [x.strip() for x in clean_evidence(raw).splitlines()] == ["go func() {", "cache[k] = v", "}()", "}"]
    assert clean_evidence("   12| x := 1\n   13| y := 2") == "x := 1\ny := 2"
    assert clean_evidence("```go\nmu.Lock()\n```") == "mu.Lock()"
    assert clean_evidence("cache[k] = v") == "cache[k] = v"


def test_locate_evidence_handles_escaped_and_fuzzy():
    from ai_cr.git_repo import locate_evidence
    content = "package svc\n\nfunc Put(k string, v int) {\n\tgo func() {\n\t\tcache[k] = v\n\t}()\n}\n"
    escaped = 'func Put(k string, v int) {\\\\n\\\\\\tgo func() {\\\\n\\\\\\t    cache[k] = v\\\\n\\\\\\t}()\\\\n}'
    assert locate_evidence(content, escaped, 3) == 3
    # 模型改写了一行，但大部分行仍能在附近找到
    assert locate_evidence(content, "go func() {\n    cache[k] = v // 写入\n}()", 4) == 4
    assert locate_evidence(content, "mu.Lock()\nother()", 4) is None
