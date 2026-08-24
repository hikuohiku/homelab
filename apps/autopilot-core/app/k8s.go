// k8s API の読み取り。コアの「目」を live のクラスタまで伸ばす部分。
//
// なぜ自前の HTTP クライアントか: client-go を入れると依存が数十 MB 増えるのに、
// ここで欲しいのは 3 本の GET だけ。net/http と encoding/json で足りる。
//
// **トークンの置き場所が設計の要点。** Pod は automountServiceAccountToken: false の
// ままで、projected volume を **このコンテナにだけ** volumeMounts している
// (deployment.yaml の kube-api-access)。volumeMounts はコンテナ単位なので、
// opencode コンテナからはトークンが見えない — Phase A で秘密をサイドカーへ追い出した
// 分離をそのまま保ったまま、k8s API へ到達できる。
//
// 権限は ClusterRole `autopilot-reader` (apps/autopilot/rbac.yaml) の get/list だけ。
// **Secret は含まれていない**。書き込み動詞も無い。
//
// トークンは projected なので有効期限付きで kubelet が差し替える。ファイルを毎回
// 読み直すのはそのため (起動時に 1 度読んで持ち回ると期限切れで 401 になる)。
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"
)

const (
	kubeTokenPath = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	kubeCAPath    = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)

// kubeClient は k8s API server への read 専用クライアント。
type kubeClient struct {
	base  string
	token func() (string, error)
	http  *http.Client
	now   func() time.Time
}

// newKubeClient は Pod 内の環境から API server への口を作る。
//
// トークンが無いときはエラーにする。「トークンが無いので空を返す」にすると、
// コアが「Pod は 0 件です」と答えてしまう (取れなかったことは異常の不在ではない)。
func newKubeClient() (*kubeClient, error) {
	base := strings.TrimSuffix(strings.TrimSpace(os.Getenv("CORE_KUBE_API")), "/")
	if base == "" {
		host := os.Getenv("KUBERNETES_SERVICE_HOST")
		port := envOr("KUBERNETES_SERVICE_PORT", "443")
		if host == "" {
			return nil, fmt.Errorf("KUBERNETES_SERVICE_HOST が無い (Pod の外で動いている?)")
		}
		if strings.Contains(host, ":") {
			host = "[" + host + "]" // IPv6
		}
		base = "https://" + host + ":" + port
	}

	transport := &http.Transport{}
	if pem, err := os.ReadFile(kubeCAPath); err == nil {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("%s を証明書として読めない", kubeCAPath)
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	} else if strings.HasPrefix(base, "https://") {
		// CA が無いのに https を喋ると、検証を切るか失敗するかの二択になる。
		// 検証を切る選択はしない
		return nil, fmt.Errorf("k8s の CA を読めない (%s): %w", kubeCAPath, err)
	}

	return &kubeClient{
		base: base,
		token: func() (string, error) {
			raw, err := os.ReadFile(kubeTokenPath)
			if err != nil {
				return "", fmt.Errorf("k8s のトークンを読めない (このコンテナに projected volume が無い?): %w", err)
			}
			return strings.TrimSpace(string(raw)), nil
		},
		http: &http.Client{Transport: transport, Timeout: 20 * time.Second},
	}, nil
}

func (k *kubeClient) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, k.base+path, nil)
	if err != nil {
		return err
	}
	if k.token != nil {
		tok, err := k.token()
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	req.Header.Set("Accept", "application/json")
	resp, err := k.http.Do(req)
	if err != nil {
		return fmt.Errorf("k8s API に届かない: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusUnauthorized {
		return fmt.Errorf("k8s API に拒否された (status=%d)。権限か token を確認すること: %s",
			resp.StatusCode, truncate(string(raw), 200))
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("k8s API が %d を返した: %s", resp.StatusCode, truncate(string(raw), 200))
	}
	return json.Unmarshal(raw, out)
}

func (k *kubeClient) stamp() string {
	now := time.Now
	if k.now != nil {
		now = k.now
	}
	return now().UTC().Format(time.RFC3339)
}

// --- 健全性レポート ---

// healthReport は ops-health-reporter が書いたレポートを ConfigMap から読む。
// 両端が同じクラスタの中に居るので GitHub を経由しない (設計 state-out-of-git Phase 5)。
// 中身は加工せず生の JSON のまま返す (要約はコアの仕事)。
func (k *kubeClient) healthReport(ctx context.Context, namespace, name, key string) (string, error) {
	var cm struct {
		Data map[string]string `json:"data"`
	}
	path := fmt.Sprintf("/api/v1/namespaces/%s/configmaps/%s", namespace, name)
	if err := k.get(ctx, path, &cm); err != nil {
		return "", err
	}
	raw := cm.Data[key]
	if strings.TrimSpace(raw) == "" {
		return "", fmt.Errorf("ConfigMap %s/%s に %s が無い", namespace, name, key)
	}
	return raw, nil
}

// --- ArgoCD Application ---

type appList struct {
	Items []struct {
		Metadata struct {
			Name string `json:"name"`
		} `json:"metadata"`
		Status struct {
			Sync struct {
				Status   string `json:"status"`
				Revision string `json:"revision"`
			} `json:"sync"`
			Health struct {
				Status  string `json:"status"`
				Message string `json:"message"`
			} `json:"health"`
		} `json:"status"`
	} `json:"items"`
}

// applications は ArgoCD Application の sync/health を live で返す。
// homelab_health (30 分ごとのレポート) と違い、いま API server が持っている値。
func (k *kubeClient) applications(ctx context.Context) (string, error) {
	var list appList
	if err := k.get(ctx, "/apis/argoproj.io/v1alpha1/applications?limit=500", &list); err != nil {
		return "", err
	}
	type row struct {
		Name     string `json:"name"`
		Sync     string `json:"sync"`
		Health   string `json:"health"`
		Revision string `json:"revision,omitempty"`
		Message  string `json:"message,omitempty"`
	}
	rows := make([]row, 0, len(list.Items))
	degraded := 0
	for _, it := range list.Items {
		r := row{
			Name:     it.Metadata.Name,
			Sync:     orUnknown(it.Status.Sync.Status),
			Health:   orUnknown(it.Status.Health.Status),
			Revision: shortRev(it.Status.Sync.Revision),
			Message:  truncate(it.Status.Health.Message, 200),
		}
		if r.Sync != "Synced" || r.Health != "Healthy" {
			degraded++
		}
		rows = append(rows, r)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].Name < rows[j].Name })
	return encodeTool(map[string]any{
		"fetched_at":   k.stamp(),
		"total":        len(rows),
		"degraded":     degraded,
		"applications": rows,
	})
}

// --- Pod ---

type podList struct {
	Items []struct {
		Metadata struct {
			Name      string `json:"name"`
			Namespace string `json:"namespace"`
		} `json:"metadata"`
		Spec struct {
			NodeName string `json:"nodeName"`
		} `json:"spec"`
		Status struct {
			Phase             string `json:"phase"`
			Reason            string `json:"reason"`
			ContainerStatuses []struct {
				Ready        bool `json:"ready"`
				RestartCount int  `json:"restartCount"`
				State        map[string]struct {
					Reason string `json:"reason"`
				} `json:"state"`
			} `json:"containerStatuses"`
		} `json:"status"`
	} `json:"items"`
}

// pods は全 namespace の Pod を 1 行ずつに畳んで返す。
//
// ここだけは取得した JSON をそのまま返せない (生の PodList は数 MB あり、
// コアの文脈に載らない)。畳む代わりに「畳んだ結果である」と分かる形にしてある。
func (k *kubeClient) pods(ctx context.Context) (string, error) {
	var list podList
	if err := k.get(ctx, "/api/v1/pods?limit=1000", &list); err != nil {
		return "", err
	}
	type row struct {
		Namespace string `json:"namespace"`
		Name      string `json:"name"`
		Phase     string `json:"phase"`
		Ready     string `json:"ready"`
		Restarts  int    `json:"restarts"`
		Node      string `json:"node,omitempty"`
		Reason    string `json:"reason,omitempty"`
	}
	rows := make([]row, 0, len(list.Items))
	unhealthy := 0
	for _, it := range list.Items {
		ready, restarts, reason := 0, 0, it.Status.Reason
		for _, cs := range it.Status.ContainerStatuses {
			if cs.Ready {
				ready++
			}
			restarts += cs.RestartCount
			for _, st := range cs.State {
				if reason == "" && st.Reason != "" && st.Reason != "Completed" {
					reason = st.Reason
				}
			}
		}
		total := len(it.Status.ContainerStatuses)
		r := row{
			Namespace: it.Metadata.Namespace,
			Name:      it.Metadata.Name,
			Phase:     orUnknown(it.Status.Phase),
			Ready:     fmt.Sprintf("%d/%d", ready, total),
			Restarts:  restarts,
			Node:      it.Spec.NodeName,
			Reason:    reason,
		}
		if r.Phase != "Succeeded" && (ready != total || r.Phase != "Running") {
			unhealthy++
		}
		rows = append(rows, r)
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Namespace != rows[j].Namespace {
			return rows[i].Namespace < rows[j].Namespace
		}
		return rows[i].Name < rows[j].Name
	})
	return encodeTool(map[string]any{
		"fetched_at": k.stamp(),
		"total":      len(rows),
		"unhealthy":  unhealthy,
		"note":       "各 Pod の要点だけに畳んである (生の PodList は文脈に載らないため)",
		"pods":       rows,
	})
}

// --- Event ---

type eventList struct {
	Items []struct {
		Type     string `json:"type"`
		Reason   string `json:"reason"`
		Message  string `json:"message"`
		Count    int    `json:"count"`
		Metadata struct {
			Namespace string `json:"namespace"`
		} `json:"metadata"`
		InvolvedObject struct {
			Kind string `json:"kind"`
			Name string `json:"name"`
		} `json:"involvedObject"`
		LastTimestamp string `json:"lastTimestamp"`
		EventTime     string `json:"eventTime"`
	} `json:"items"`
}

// maxEvents は返す件数の上限。新しい方から残す。
const maxEvents = 60

// events は Normal でない (= Warning 等の) Event を新しい順で返す。
// OOMKill・スケジュール失敗・probe 失敗といった「異常が起きた瞬間」がここに出る。
func (k *kubeClient) events(ctx context.Context) (string, error) {
	var list eventList
	if err := k.get(ctx, "/api/v1/events?fieldSelector="+url.QueryEscape("type!=Normal")+"&limit=500", &list); err != nil {
		return "", err
	}
	type row struct {
		At      string `json:"at"`
		Type    string `json:"type"`
		Reason  string `json:"reason"`
		Object  string `json:"object"`
		Count   int    `json:"count"`
		Message string `json:"message"`
	}
	rows := make([]row, 0, len(list.Items))
	for _, it := range list.Items {
		at := it.LastTimestamp
		if at == "" {
			at = it.EventTime
		}
		rows = append(rows, row{
			At:     at,
			Type:   it.Type,
			Reason: it.Reason,
			Object: fmt.Sprintf("%s/%s %s", it.Metadata.Namespace,
				strings.ToLower(it.InvolvedObject.Kind), it.InvolvedObject.Name),
			Count:   it.Count,
			Message: truncate(it.Message, 300),
		})
	}
	// 新しい順。RFC3339 は辞書順が時刻順なので文字列比較でよい
	sort.Slice(rows, func(i, j int) bool { return rows[i].At > rows[j].At })
	truncated := false
	if len(rows) > maxEvents {
		rows, truncated = rows[:maxEvents], true
	}
	return encodeTool(map[string]any{
		"fetched_at": k.stamp(),
		"returned":   len(rows),
		"truncated":  truncated,
		"note":       "type!=Normal のみ。Event は既定で 1 時間程度しか残らないので、無いことは平穏の証拠にならない",
		"events":     rows,
	})
}

// --- 小物 ---

func encodeTool(v any) (string, error) {
	raw, err := json.MarshalIndent(v, "", " ")
	if err != nil {
		return "", err
	}
	return clip(string(raw)), nil
}

func orUnknown(s string) string {
	if strings.TrimSpace(s) == "" {
		return "Unknown"
	}
	return s
}

func shortRev(rev string) string {
	if len(rev) > 8 {
		return rev[:8]
	}
	return rev
}
