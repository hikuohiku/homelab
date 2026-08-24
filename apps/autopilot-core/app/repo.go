// リポジトリの作業コピー。コアが main の中身を自分で読めるようにする。
//
// driver が PVC 上に clone を持ち、周期的に main へ合わせる。opencode コンテナには
// **read-only で** mount してある (deployment.yaml の state / subPath: repo) ので、
// コアは読めるが書けない。設計 D30「コアは git に書かない」を allowlist ではなく
// コンテナの性質として持たせるための形。push 用の credential もコア側には無い。
//
// 更新の作法:
//
//	git init -b main         (最初の 1 回だけ。ディレクトリ自体は消さない)
//	git fetch origin main
//	git reset --hard FETCH_HEAD
//	git clean -fd
//
// **ディレクトリを消して clone し直さない**のが要点。opencode 側は subPath の
// bind mount なので、ディレクトリを作り直すと mount が古い inode を指したままになり、
// コアから中身が消えたように見える。
//
// 失敗しても driver は止まらない。作業コピーが古いことは人間への返事を止める理由に
// ならないし、この driver は所有者の「止めて」を運ぶ唯一の経路でもある。
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// gitRunner は git を 1 回呼ぶ。テストでは差し替えて呼び出し列を検査する。
type gitRunner func(ctx context.Context, args ...string) (string, error)

type repoSyncer struct {
	dir  string
	url  string
	ref  string
	home string
	run  gitRunner
}

// repoWorkDir は main の作業コピーの置き場。silence.go も同じ場所から
// ops/rules.json を読むので、既定値の解釈をここ 1 箇所に持つ。
func repoWorkDir(cfg *config) string {
	return envOr("CORE_REPO_DIR", filepath.Join(cfg.stateDir, "repo"))
}

func newRepoSyncer(cfg *config) *repoSyncer {
	r := &repoSyncer{
		dir:  repoWorkDir(cfg),
		url:  envOr("CORE_REPO_URL", "https://github.com/"+cfg.repo+".git"),
		ref:  envOr("CORE_REPO_REF", "main"),
		home: cfg.stateDir,
	}
	r.run = r.exec
	return r
}

// exec は git を実際に起動する。
//
// credential は渡さない。homelab は public なので anonymous な https で足り、
// トークンを渡すと remote URL や credential store 経由で PVC に残りうる
// (その PVC はコアからも見える)。GIT_TERMINAL_PROMPT=0 は、認証を求められたときに
// 端末待ちで固まらせないため — 誰も答えられない環境で待つのは死と同じ。
func (r *repoSyncer) exec(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", r.dir}, args...)...)
	cmd.Env = append(os.Environ(),
		"HOME="+r.home,
		"GIT_TERMINAL_PROMPT=0",
		"GIT_CONFIG_NOSYSTEM=1",
		"GIT_ASKPASS=/bin/true",
	)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s: %w: %s", strings.Join(args, " "), err, truncate(strings.TrimSpace(string(out)), 300))
	}
	return strings.TrimSpace(string(out)), nil
}

// sync は作業コピーを ref の先端に合わせ、その commit を返す。
func (r *repoSyncer) sync(ctx context.Context) (string, error) {
	if err := os.MkdirAll(r.dir, 0o755); err != nil {
		return "", fmt.Errorf("作業コピーの置き場を作れない: %w", err)
	}
	if _, err := os.Stat(filepath.Join(r.dir, ".git")); err != nil {
		if _, err := r.run(ctx, "init", "-q", "-b", r.ref); err != nil {
			return "", err
		}
	}
	// remote は毎回上書きする (add は 2 回目に失敗するので set-url で冪等にする)。
	if _, err := r.run(ctx, "remote", "set-url", "origin", r.url); err != nil {
		if _, err := r.run(ctx, "remote", "add", "origin", r.url); err != nil {
			return "", err
		}
	}
	if _, err := r.run(ctx, "fetch", "--no-tags", "--prune", "origin", r.ref); err != nil {
		return "", err
	}
	// FETCH_HEAD へ強制で合わせる。ローカルに何が残っていても main の姿に戻る
	if _, err := r.run(ctx, "reset", "-q", "--hard", "FETCH_HEAD"); err != nil {
		return "", err
	}
	if _, err := r.run(ctx, "clean", "-qfd"); err != nil {
		return "", err
	}
	return r.run(ctx, "rev-parse", "--short", "HEAD")
}

// repoSyncInterval は作業コピーを合わせ直す間隔。
// main は 1 時間に数回しか動かないので、詰めても意味が無い。
func repoSyncInterval() time.Duration {
	return time.Duration(envOrInt("CORE_REPO_SYNC_SECONDS", 300)) * time.Second
}

// runRepoSyncLoop は作業コピーの更新を回し続ける。戻らない。
//
// メインループと別の goroutine で回すのは、初回の clone (数十秒〜) が
// 人間への返事を待たせないため。
func runRepoSyncLoop(ctx context.Context, cfg *config) {
	r := newRepoSyncer(cfg)
	log.Printf("作業コピーの同期を開始 (dir=%s ref=%s 間隔=%s)", r.dir, r.ref, repoSyncInterval())
	last := ""
	for {
		head, err := r.sync(ctx)
		switch {
		case err != nil:
			// 古い作業コピーで走り続ける方が、コアが repo を全く読めないより良い
			log.Printf("作業コピーを更新できない (次の周回で再試行): %v", err)
		case head != last:
			if last == "" {
				log.Printf("作業コピーを用意した (%s)", head)
			} else {
				log.Printf("作業コピーを更新: %s → %s", last, head)
			}
			last = head
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(repoSyncInterval()):
		}
	}
}
