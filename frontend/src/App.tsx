import React, { useState, useEffect, useRef } from 'react';

interface CircuitInfo {
  name: string;
  socks_port: number;
  control_port: number;
  state: string;
  requests_total: number;
  requests_ok: number;
  requests_failed: number;
  success_rate: number;
  avg_latency_ms: number;
  echo_events: number;
  last_error: string | null;
}

interface ProxyStats {
  proxy: {
    host: string;
    port: number;
    parallel_race: boolean;
    num_circuits: number;
  };
  retry: {
    total_requests: number;
    total_echoes: number;
    echo_rate: number;
  };
  circuits: CircuitInfo[];
}

interface LogEntry {
  id: string;
  timestamp: string;
  method: string;
  url: string;
  status: string;
  circuit: string;
  latencyMs: number;
}

export default function App() {
  const [stats, setStats] = useState<ProxyStats | null>(null);
  const [isOnline, setIsOnline] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [testUrl, setTestUrl] = useState('https://check.torproject.org/api/ip');
  const [testResult, setTestResult] = useState<string>('');
  const [testLoading, setTestLoading] = useState(false);
  const [rotatingId, setRotatingId] = useState<string | null>(null);

  const statsInterval = useRef<any>(null);

  // Fetch stats from TorProxy
  const fetchStats = async () => {
    try {
      const response = await fetch('http://localhost:8080/__torproxy__/stats');
      if (response.ok) {
        const data = await response.json();
        setStats(data);
        setIsOnline(true);
      } else {
        setIsOnline(false);
      }
    } catch (err) {
      setIsOnline(false);
    }
  };

  useEffect(() => {
    fetchStats();
    statsInterval.current = setInterval(fetchStats, 2000);
    return () => clearInterval(statsInterval.current);
  }, []);

  // Trigger circuit rotation
  const handleRotate = async (index: number | 'all') => {
    const circuitName = index === 'all' ? 'all' : `circuit-${index}`;
    setRotatingId(circuitName);
    
    // Add request to log
    addLog('POST', `/__torproxy__/rotate?index=${index}`, 'ROTATING', circuitName, 0);

    try {
      const start = performance.now();
      const response = await fetch(`http://localhost:8080/__torproxy__/rotate?index=${index}`);
      const duration = Math.round(performance.now() - start);
      
      if (response.ok) {
        const data = await response.json();
        addLog('POST', `/__torproxy__/rotate?index=${index}`, 'SUCCESS', circuitName, duration);
        fetchStats();
      } else {
        addLog('POST', `/__torproxy__/rotate?index=${index}`, 'FAILED', circuitName, duration);
      }
    } catch (err) {
      addLog('POST', `/__torproxy__/rotate?index=${index}`, 'ERROR', circuitName, 0);
    } finally {
      setRotatingId(null);
    }
  };

  // Run proxy routing test
  const handleTestRoute = async () => {
    if (!testUrl) return;
    setTestLoading(true);
    setTestResult('Routing request through Tor network...');
    
    const requestUrl = `http://localhost:8080/${testUrl}`;
    addLog('GET', testUrl, 'FETCHING', 'RACING', 0);

    try {
      const start = performance.now();
      const response = await fetch(requestUrl);
      const duration = Math.round(performance.now() - start);
      const text = await response.text();
      
      let parsed = text;
      try {
        // Pretty print if JSON
        const json = JSON.parse(text);
        parsed = JSON.stringify(json, null, 2);
      } catch (e) {}

      setTestResult(parsed);
      addLog('GET', testUrl, response.status.toString(), 'RACING', duration);
      fetchStats();
    } catch (err: any) {
      setTestResult(`Failed to route: ${err.message}`);
      addLog('GET', testUrl, 'ERROR', 'RACING', 0);
    } finally {
      setTestLoading(false);
    }
  };

  // Local console log helper
  const addLog = (method: string, url: string, status: string, circuit: string, latencyMs: number) => {
    const newEntry: LogEntry = {
      id: Math.random().toString(),
      timestamp: new Date().toLocaleTimeString(),
      method,
      url,
      status,
      circuit,
      latencyMs,
    };
    setLogs((prev) => [newEntry, ...prev].slice(0, 50));
  };

  return (
    <div className="dashboard-container">
      {/* Header */}
      <header className="header">
        <div>
          <h1>TorProxy Control Center</h1>
          <p style={{ color: 'var(--text-muted)', marginTop: '4px' }}>
            Multi-Circuit Parallel Racing Proxy Server
          </p>
        </div>
        <div className={`status-badge ${isOnline ? 'online' : 'offline'}`}>
          <span className="status-dot"></span>
          {isOnline ? 'PROXY ONLINE' : 'PROXY OFFLINE'}
        </div>
      </header>

      {/* Stats Cards */}
      <div className="stats-grid">
        <div className="glass-panel stat-card">
          <span className="stat-label">Forwarded Requests</span>
          <span className="stat-value">{stats?.retry.total_requests ?? 0}</span>
          <span className="stat-desc">Total HTTP/HTTPS requests processed</span>
        </div>
        <div className="glass-panel stat-card">
          <span className="stat-label">Echo Rate</span>
          <span className="stat-value">
            {stats ? `${(stats.retry.echo_rate * 100).toFixed(1)}%` : '0.0%'}
          </span>
          <span className="stat-desc" style={{ color: 'var(--color-ready)' }}>
            Target: 0% (Retry circuit safety)
          </span>
        </div>
        <div className="glass-panel stat-card">
          <span className="stat-label">Active Circuits</span>
          <span className="stat-value">
            {stats?.circuits.filter((c) => c.state === 'READY').length ?? 0}/
            {stats?.proxy.num_circuits ?? 0}
          </span>
          <span className="stat-desc">Independent Tor SOCKS5 instances</span>
        </div>
        <div className="glass-panel stat-card">
          <span className="stat-label">Circuit Strategy</span>
          <span className="stat-value" style={{ fontSize: '1.8rem', paddingTop: '10px' }}>
            {stats?.proxy.parallel_race ? 'PARALLEL RACE' : 'SEQUENTIAL'}
          </span>
          <span className="stat-desc">Fastest circuit wins connection</span>
        </div>
      </div>

      {/* Grid of Tor instances */}
      <section className="circuits-section">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
          <h2>Tor Circuit Instances</h2>
          <button 
            className="rotate-btn" 
            style={{ width: 'auto', padding: '8px 16px' }}
            disabled={!isOnline || rotatingId !== null}
            onClick={() => handleRotate('all')}
          >
            {rotatingId === 'all' ? 'Rotating All...' : '🔄 Rotate All Identities'}
          </button>
        </div>

        <div className="circuits-grid">
          {stats?.circuits.map((c, index) => {
            const isRotating = rotatingId === c.name;
            return (
              <div key={c.name} className={`glass-panel circuit-card ${c.state}`}>
                <div className="circuit-header">
                  <span className="circuit-name">{c.name}</span>
                  <span className={`state-badge ${c.state}`}>{c.state}</span>
                </div>

                <div className="circuit-details">
                  <div className="detail-item">
                    <span className="detail-label">SOCKS5 Port</span>
                    <span className="detail-value">{c.socks_port}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Control Port</span>
                    <span className="detail-value">{c.control_port}</span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Ok / Fail</span>
                    <span className="detail-value">
                      <span style={{ color: 'var(--color-ready)' }}>{c.requests_ok}</span>
                      {' / '}
                      <span style={{ color: 'var(--color-dead)' }}>{c.requests_failed}</span>
                    </span>
                  </div>
                  <div className="detail-item">
                    <span className="detail-label">Avg Latency</span>
                    <span className="detail-value">
                      {c.avg_latency_ms > 0 ? `${Math.round(c.avg_latency_ms)} ms` : 'N/A'}
                    </span>
                  </div>
                </div>

                <button
                  className="rotate-btn"
                  disabled={!isOnline || rotatingId !== null}
                  onClick={() => handleRotate(index)}
                >
                  {isRotating ? 'Rotating...' : '🔄 New Identity (NEWNYM)'}
                </button>
              </div>
            );
          })}
        </div>
      </section>

      {/* Action Tester & Logger Grid */}
      <div className="tester-section">
        {/* Route Tester */}
        <div className="glass-panel tester-card">
          <div className="tester-title">Request Route Tester</div>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '16px' }}>
            Route an HTTP/HTTPS GET request through the proxy server to verify active Tor exit node IP.
          </p>

          <div className="input-group">
            <input
              type="text"
              className="url-input"
              value={testUrl}
              onChange={(e) => setTestUrl(e.target.value)}
              placeholder="e.g. https://check.torproject.org/api/ip"
              disabled={!isOnline || testLoading}
            />
            <button
              className="test-btn"
              onClick={handleTestRoute}
              disabled={!isOnline || testLoading}
            >
              {testLoading ? 'Testing...' : 'Fetch via Proxy'}
            </button>
          </div>

          <div className="result-box">
            {testResult || 'Response output will show here.'}
          </div>
        </div>

        {/* Console Logger */}
        <div className="glass-panel console-card">
          <div className="console-header">
            <span style={{ fontWeight: 600 }}>Console Log</span>
            <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              Live Request Stream
            </span>
          </div>

          <div className="console-lines">
            {logs.length === 0 ? (
              <div style={{ color: 'var(--text-muted)', padding: '8px' }}>
                Waiting for incoming request logs...
              </div>
            ) : (
              logs.map((log) => (
                <div key={log.id} className="console-line">
                  <span className="line-time">[{log.timestamp}]</span>
                  <span className={`line-status ${log.status.startsWith('2') || log.status === 'SUCCESS' ? 'ok' : 'err'}`}>
                    {log.status}
                  </span>
                  <span className="line-circuit">{log.circuit}</span>
                  <span className="line-msg" style={{ wordBreak: 'break-all' }}>
                    {log.method} {log.url} {log.latencyMs > 0 ? `(${log.latencyMs}ms)` : ''}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
