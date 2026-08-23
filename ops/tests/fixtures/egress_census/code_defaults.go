package main

// 合成 fixture: Go コードの既定値 URL。実リポジトリの apps/*/app/main.go の形。
// コメント内の https://example.com/docs は抽出しない。

func envOr(key, fallback string) string {
	return fallback
}

func config() config {
	return config{
		botAPI:    strings.TrimSuffix(envOr("BOT_API", "https://api.example.org"), "/"),
		localPort: envOr("LOCAL_URL", "http://127.0.0.1:8080"),
	}
}
