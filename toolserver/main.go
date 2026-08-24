// AgentDesk tool server.
//
// A small, long-running Go service that owns read access to incident
// telemetry (alerts, logs, metrics, deploys, config changes, topology) and
// exposes it as narrow, filterable HTTP endpoints. The Python agent harness
// talks to this server; the LLM never touches raw fixture files.
//
// Why Go here: this is the piece that would sit in front of real telemetry
// backends (Loki/Prometheus/deploy API) in production — a concurrent,
// low-overhead fan-out proxy is a natural Go job, while the fast-moving
// agent logic stays in Python.
//
// Ground truth labels (truth.json) are deliberately NOT loaded or served:
// the agent must earn its answer from telemetry, and eval reads labels
// directly from disk.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

type LogLine struct {
	TS      string `json:"ts"`
	Service string `json:"service"`
	Level   string `json:"level"`
	Msg     string `json:"msg"`
}

type MetricPoint struct {
	TS string  `json:"ts"`
	V  float64 `json:"v"`
}

type Fixtures struct {
	Incident      map[string]any                      `json:"incident"`
	Alerts        []map[string]any                    `json:"alerts"`
	Topology      map[string]any                      `json:"topology"`
	Logs          []LogLine                           `json:"logs"`
	Metrics       map[string]map[string][]MetricPoint `json:"metrics"`
	Deploys       []map[string]any                    `json:"deploys"`
	ConfigChanges []map[string]any                    `json:"config_changes"`
}

type Server struct {
	incidents map[string]*Fixtures
}

func loadScenarios(dir string) (map[string]*Fixtures, error) {
	out := map[string]*Fixtures{}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		fp := filepath.Join(dir, e.Name(), "fixtures.json")
		raw, err := os.ReadFile(fp)
		if err != nil {
			log.Printf("skip %s: %v", e.Name(), err)
			continue
		}
		var fx Fixtures
		if err := json.Unmarshal(raw, &fx); err != nil {
			return nil, fmt.Errorf("parse %s: %w", fp, err)
		}
		out[e.Name()] = &fx
		log.Printf("loaded %s (%d log lines, %d deploys, %d config changes)",
			e.Name(), len(fx.Logs), len(fx.Deploys), len(fx.ConfigChanges))
	}
	return out, nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func parseTime(s string) (time.Time, bool) {
	if s == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

// incidentFrom extracts the {id} path segment: /api/incidents/{id}/rest...
func (s *Server) incidentFrom(w http.ResponseWriter, r *http.Request) (*Fixtures, string, bool) {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	// parts: ["api", "incidents", "{id}", ...]
	if len(parts) < 3 {
		writeErr(w, http.StatusNotFound, "incident id required")
		return nil, "", false
	}
	id := parts[2]
	fx, ok := s.incidents[id]
	if !ok {
		writeErr(w, http.StatusNotFound, "unknown incident "+id)
		return nil, "", false
	}
	rest := ""
	if len(parts) > 3 {
		rest = strings.Join(parts[3:], "/")
	}
	return fx, rest, true
}

func (s *Server) handleIncidents(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/api/incidents" || r.URL.Path == "/api/incidents/" {
		ids := make([]string, 0, len(s.incidents))
		for id := range s.incidents {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		summaries := []map[string]any{}
		for _, id := range ids {
			summaries = append(summaries, s.incidents[id].Incident)
		}
		writeJSON(w, http.StatusOK, map[string]any{"incidents": summaries})
		return
	}

	fx, rest, ok := s.incidentFrom(w, r)
	if !ok {
		return
	}
	q := r.URL.Query()

	switch rest {
	case "":
		writeJSON(w, http.StatusOK, map[string]any{"incident": fx.Incident, "alerts": fx.Alerts})

	case "topology":
		writeJSON(w, http.StatusOK, fx.Topology)

	case "logs":
		service := q.Get("service")
		level := strings.ToUpper(q.Get("level"))
		grep := strings.ToLower(q.Get("grep"))
		since, hasSince := parseTime(q.Get("since"))
		until, hasUntil := parseTime(q.Get("until"))
		limit := 50
		if v, err := strconv.Atoi(q.Get("limit")); err == nil && v > 0 && v <= 500 {
			limit = v
		}
		matched := []LogLine{}
		total := 0
		for _, l := range fx.Logs {
			if service != "" && l.Service != service {
				continue
			}
			if level != "" && l.Level != level {
				continue
			}
			if grep != "" && !strings.Contains(strings.ToLower(l.Msg), grep) {
				continue
			}
			ts, _ := time.Parse(time.RFC3339, l.TS)
			if hasSince && ts.Before(since) {
				continue
			}
			if hasUntil && ts.After(until) {
				continue
			}
			total++
			matched = append(matched, l)
		}
		// Return the most recent `limit` lines (tail semantics, like real log UIs).
		if len(matched) > limit {
			matched = matched[len(matched)-limit:]
		}
		writeJSON(w, http.StatusOK, map[string]any{"total_matched": total, "returned": len(matched), "lines": matched})

	case "metrics":
		service := q.Get("service")
		name := q.Get("name")
		if service == "" {
			writeErr(w, http.StatusBadRequest, "service query param required")
			return
		}
		svcMetrics, ok := fx.Metrics[service]
		if !ok {
			writeErr(w, http.StatusNotFound, "no metrics for service "+service)
			return
		}
		since, hasSince := parseTime(q.Get("since"))
		until, hasUntil := parseTime(q.Get("until"))
		filter := func(pts []MetricPoint) []MetricPoint {
			out := []MetricPoint{}
			for _, p := range pts {
				ts, _ := time.Parse(time.RFC3339, p.TS)
				if hasSince && ts.Before(since) {
					continue
				}
				if hasUntil && ts.After(until) {
					continue
				}
				out = append(out, p)
			}
			return out
		}
		result := map[string][]MetricPoint{}
		if name != "" {
			pts, ok := svcMetrics[name]
			if !ok {
				writeErr(w, http.StatusNotFound, "no metric "+name+" for "+service)
				return
			}
			result[name] = filter(pts)
		} else {
			for n, pts := range svcMetrics {
				result[n] = filter(pts)
			}
		}
		writeJSON(w, http.StatusOK, map[string]any{"service": service, "series": result})

	case "deploys":
		service := q.Get("service")
		since, hasSince := parseTime(q.Get("since"))
		out := []map[string]any{}
		for _, d := range fx.Deploys {
			if service != "" && d["service"] != service {
				continue
			}
			if hasSince {
				ts, _ := time.Parse(time.RFC3339, d["ts"].(string))
				if ts.Before(since) {
					continue
				}
			}
			out = append(out, d)
		}
		writeJSON(w, http.StatusOK, map[string]any{"deploys": out})

	case "config_changes":
		service := q.Get("service")
		since, hasSince := parseTime(q.Get("since"))
		out := []map[string]any{}
		for _, c := range fx.ConfigChanges {
			if service != "" && c["service"] != service {
				continue
			}
			if hasSince {
				ts, _ := time.Parse(time.RFC3339, c["ts"].(string))
				if ts.Before(since) {
					continue
				}
			}
			out = append(out, c)
		}
		writeJSON(w, http.StatusOK, map[string]any{"config_changes": out})

	default:
		writeErr(w, http.StatusNotFound, "unknown resource "+rest)
	}
}

func logMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s (%s)", r.Method, r.URL.RequestURI(), time.Since(start).Round(time.Millisecond))
	})
}

func main() {
	addr := flag.String("addr", ":8077", "listen address")
	dir := flag.String("scenarios", "scenarios", "scenario fixtures directory")
	flag.Parse()

	incidents, err := loadScenarios(*dir)
	if err != nil {
		log.Fatalf("load scenarios: %v", err)
	}
	if len(incidents) == 0 {
		log.Fatal("no scenarios loaded; run scripts/generate_scenarios.py first")
	}

	s := &Server{incidents: incidents}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/incidents", s.handleIncidents)
	mux.HandleFunc("/api/incidents/", s.handleIncidents)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	log.Printf("agentdesk toolserver listening on %s (%d incidents)", *addr, len(incidents))
	log.Fatal(http.ListenAndServe(*addr, logMiddleware(mux)))
}
