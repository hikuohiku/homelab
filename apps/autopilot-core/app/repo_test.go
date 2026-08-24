// 作業コピーの更新手順を固定する。
//
// 守りたいのは 2 点:
//   - ディレクトリを消して clone し直さないこと (opencode 側の subPath mount が
//     古い inode を指したままになり、コアから中身が消えたように見える)
//   - main へ強制で合わせること。ローカルに何が残っていても main の姿に戻す
package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func recordingSyncer(t *testing.T, fail map[string]error) (*repoSyncer, *[]string) {
	t.Helper()
	calls := []string{}
	r := &repoSyncer{dir: filepath.Join(t.TempDir(), "repo"), url: "https://example.invalid/r.git", ref: "main"}
	r.run = func(_ context.Context, args ...string) (string, error) {
		joined := strings.Join(args, " ")
		calls = append(calls, joined)
		if err, ok := fail[args[0]]; ok {
			return "", err
		}
		return "abc1234", nil
	}
	return r, &calls
}

func TestSyncBootstrapsThenForcesToRemote(t *testing.T) {
	r, calls := recordingSyncer(t, nil)

	head, err := r.sync(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if head != "abc1234" {
		t.Fatalf("HEAD を返すべき: %q", head)
	}

	got := strings.Join(*calls, "\n")
	for _, want := range []string{
		"init -q -b main",
		"remote set-url origin https://example.invalid/r.git",
		"fetch --no-tags --prune origin main",
		"reset -q --hard FETCH_HEAD",
		"clean -qfd",
		"rev-parse --short HEAD",
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("`git %s` を呼ぶべき:\n%s", want, got)
		}
	}
	// 作業コピーを持つディレクトリを消す手順があってはいけない
	for _, forbidden := range []string{"clone", "push", "commit"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("%s は使わない: %s", forbidden, got)
		}
	}
	if _, err := os.Stat(r.dir); err != nil {
		t.Fatalf("置き場を作るべき: %v", err)
	}
}

func TestSyncSkipsInitWhenAlreadyCloned(t *testing.T) {
	r, calls := recordingSyncer(t, nil)
	if err := os.MkdirAll(filepath.Join(r.dir, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}

	if _, err := r.sync(context.Background()); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(strings.Join(*calls, "\n"), "init") {
		t.Fatalf("2 回目以降は init しない: %v", *calls)
	}
}

func TestSyncFallsBackToRemoteAddOnFirstRun(t *testing.T) {
	// remote が無い状態では set-url が失敗する。そこで諦めない
	r, calls := recordingSyncer(t, map[string]error{"remote": errors.New("no such remote")})
	r.run = func(ctx context.Context, args ...string) (string, error) {
		*calls = append(*calls, strings.Join(args, " "))
		if args[0] == "remote" && args[1] == "set-url" {
			return "", errors.New("no such remote")
		}
		return "abc1234", nil
	}

	if _, err := r.sync(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(*calls, "\n"), "remote add origin") {
		t.Fatalf("remote add へ落ちるべき: %v", *calls)
	}
}

func TestSyncReportsFetchFailure(t *testing.T) {
	// 取れなかったことを成功にしない。古い作業コピーのまま「最新です」と言わせない
	r, _ := recordingSyncer(t, map[string]error{"fetch": errors.New("network down")})

	if _, err := r.sync(context.Background()); err == nil {
		t.Fatal("fetch の失敗は error で返すべき")
	}
}

func TestRepoSyncerDefaultsToStateDir(t *testing.T) {
	r := newRepoSyncer(&config{stateDir: "/data", repo: "hikuohiku/homelab"})
	if r.dir != "/data/repo" {
		t.Fatalf("PVC 上に置くべき: %q", r.dir)
	}
	if r.url != "https://github.com/hikuohiku/homelab.git" {
		t.Fatalf("匿名 https で取るべき (トークンを PVC に残さない): %q", r.url)
	}
	if r.ref != "main" {
		t.Fatalf("main を見るべき: %q", r.ref)
	}
}
