'use client';

import React, { useEffect, useState } from 'react';
import { 
    AlertTriangle, CheckCircle, XCircle, Clock, 
    BarChart3, Activity, ShieldAlert, FileJson,
    RefreshCw, AlertOctagon
} from 'lucide-react';

export default function QuantValidationDashboard() {
    const [data, setData] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

    const fetchData = async () => {
        try {
            const res = await fetch('/api/quant-validation');
            if (!res.ok) throw new Error('Failed to fetch data');
            const json = await res.json();
            setData(json);
            setLastRefresh(new Date());
        } catch (err: any) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
        const interval = setInterval(fetchData, 60000);
        return () => clearInterval(interval);
    }, []);

    if (loading && !data) {
        return (
            <div className="flex items-center justify-center min-h-screen bg-gray-900 text-white">
                <RefreshCw className="animate-spin w-8 h-8 text-blue-500" />
            </div>
        );
    }

    if (error) {
        return (
            <div className="flex items-center justify-center min-h-screen bg-gray-900 text-white p-4">
                <div className="bg-red-900/50 p-6 rounded-xl border border-red-500 max-w-md w-full">
                    <AlertOctagon className="w-12 h-12 text-red-500 mb-4 mx-auto" />
                    <h2 className="text-xl font-bold text-center mb-2">Error Loading Dashboard</h2>
                    <p className="text-red-200 text-center">{error}</p>
                    <button onClick={fetchData} className="mt-4 w-full bg-red-600 hover:bg-red-700 py-2 rounded-lg font-medium transition-colors">
                        Retry
                    </button>
                </div>
            </div>
        );
    }

    if (!data) return null;

    const { overview, sports, weather, execution, clv, rejections, data_consistency } = data;

    // Computed Alerts
    const alerts = [];
    if (overview.total_paper_fills > overview.total_would_trade) {
        alerts.push("Paper fills exceed would_trade count (unexplained)");
    }
    if (overview.missing_timestamps > 0) {
        alerts.push(`Found ${overview.missing_timestamps} missing timestamps`);
    }
    if (overview.live_orders_submitted > 0) {
        alerts.push(`CRITICAL: ${overview.live_orders_submitted} Live orders submitted!`);
    }
    if (overview.calibration_status.includes('uncalibrated_shadow') && overview.current_mode === 'live') {
        alerts.push("Uncalibrated model running in live mode");
    }
    if (weather.nowcast_impossible_bucket_leaks > 0) {
        alerts.push(`CRITICAL: ${weather.nowcast_impossible_bucket_leaks} Nowcast impossible bucket leaks!`);
    }
    if (weather.probability_vector_validation_failures > 0) {
        alerts.push(`${weather.probability_vector_validation_failures} Probability vector validation failures`);
    }
    if (execution.stale_orderbooks_rejected > 0) {
        alerts.push(`CRITICAL: ${execution.stale_orderbooks_rejected} stale orderbooks rejected/accepted?`);
    }
    if (execution.effective_cost_gte_1_rejected > 0) {
        alerts.push(`CRITICAL: ${execution.effective_cost_gte_1_rejected} effective_cost >= 1 issues`);
    }
    if (execution.fee_model_unavailable > 0) {
        alerts.push(`CRITICAL: Fee model unavailable for ${execution.fee_model_unavailable} markets`);
    }

    const isOldOrMissing = !overview.last_sync_time || 
                           (new Date().getTime() - new Date(overview.last_sync_time).getTime() > 24 * 60 * 60 * 1000);

    const Card = ({ title, value, baseline, status }: any) => (
        <div className="bg-gray-800 rounded-xl p-4 border border-gray-700 hover:border-gray-600 transition-colors">
            <h3 className="text-gray-400 text-sm font-medium mb-1">{title}</h3>
            <div className="flex items-end gap-2">
                <span className={`text-2xl font-bold ${status === 'red' ? 'text-red-500' : status === 'yellow' ? 'text-yellow-400' : status === 'green' ? 'text-green-400' : 'text-white'}`}>
                    {value}
                </span>
            </div>
            {baseline && <p className="text-xs text-gray-500 mt-2">Goal: {baseline}</p>}
        </div>
    );

    return (
        <div className="min-h-screen bg-gray-900 text-gray-100 p-6 font-sans">
            <div className="max-w-7xl mx-auto space-y-8">
                
                {/* Header */}
                <div className="flex items-center justify-between">
                    <div>
                        <h1 className="text-3xl font-bold text-white tracking-tight flex items-center gap-3">
                            <Activity className="text-blue-500" />
                            Quant Validation Dashboard
                        </h1>
                        <p className="text-gray-400 mt-1 flex items-center gap-4 text-sm">
                            <span>Last Refresh: {lastRefresh?.toLocaleTimeString()}</span>
                            <span>Report Date: {new Date(overview.last_sync_time).toLocaleString()}</span>
                            <span>Status: {overview.last_sync_status}</span>
                        </p>
                    </div>
                    <button onClick={fetchData} className="bg-gray-800 hover:bg-gray-700 border border-gray-700 p-2 rounded-lg transition-colors">
                        <RefreshCw className="w-5 h-5 text-gray-300" />
                    </button>
                </div>

                {/* Artifact Banner */}
                <div className="bg-gray-800/80 border border-blue-500/30 rounded-xl p-4 flex items-start gap-3 shadow-lg">
                    <div className="mt-1">
                        <FileJson className="w-5 h-5 text-blue-400" />
                    </div>
                    <div>
                        <h2 className="text-blue-300 font-bold mb-1">Data source: Local files</h2>
                        {isOldOrMissing ? (
                            <div className="text-sm text-yellow-300 font-mono mt-2 bg-gray-900/50 p-3 rounded border border-yellow-500/20">
                                No recent local validation artifact imported.<br/>
                                Download the latest GitHub Actions artifact and run:<br/>
                                <span className="text-white mt-1 block">python scripts/import_quant_artifact.py &lt;artifact.zip&gt;</span>
                            </div>
                        ) : (
                            <p className="text-sm text-gray-400">
                                Reading validation logs from the local filesystem.
                            </p>
                        )}
                    </div>
                </div>

                {/* Alerts Section */}
                {alerts.length > 0 && (
                    <div className="bg-red-900/30 border border-red-500/50 rounded-xl p-4 shadow-lg shadow-red-900/20">
                        <h2 className="text-red-400 font-bold mb-3 flex items-center gap-2">
                            <ShieldAlert className="w-5 h-5" /> Active Safety Alerts
                        </h2>
                        <ul className="space-y-2">
                            {alerts.map((a, i) => (
                                <li key={i} className="flex items-center gap-2 text-red-200 text-sm bg-red-950/50 p-2 rounded-lg">
                                    <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                                    {a}
                                </li>
                            ))}
                        </ul>
                    </div>
                )}

                {/* Overview */}
                <section className="space-y-4">
                    <h2 className="text-xl font-semibold text-white flex items-center gap-2">
                        <BarChart3 className="w-5 h-5 text-indigo-400" /> Overview
                    </h2>
                    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
                        <Card title="Current Mode" value={overview.current_mode} />
                        <Card title="Live Orders Submitted" value={overview.live_orders_submitted} baseline="0" status={overview.live_orders_submitted > 0 ? 'red' : 'green'} />
                        <Card title="Blocked Live Attempts" value={overview.live_order_attempts_blocked} />
                        <Card title="Total Predictions" value={overview.total_predictions} />
                        <Card title="Total Rejections" value={overview.total_rejections} />
                        <Card title="Total Would-Trade" value={overview.total_would_trade} />
                        <Card title="Total Paper Fills" value={overview.total_paper_fills} />
                        <Card title="Missing Timestamps" value={overview.missing_timestamps} baseline="0" status={overview.missing_timestamps > 0 ? 'red' : 'green'} />
                        <Card title="Assumed Timestamps" value={overview.assumed_timestamps} baseline="0 preferred" status={overview.assumed_timestamps > 0 ? 'yellow' : 'green'} />
                        <Card title="Avg 15m CLV" value={overview.average_15m_clv.toFixed(4)} baseline=">= 0 over large sample" status={clv._15m_checkpoints_completed < 30 ? 'white' : overview.average_15m_clv >= 0 ? 'green' : 'red'} />
                        <Card title="Avg 1h CLV" value={overview.average_1h_clv.toFixed(4)} baseline=">= 0 over large sample" status={clv._1h_checkpoints_completed < 30 ? 'white' : overview.average_1h_clv >= 0 ? 'green' : 'red'} />
                        <Card title="Calibration Status" value={overview.calibration_status} baseline="uncalibrated_shadow expected" status={overview.calibration_status.includes('uncalibrated') ? 'yellow' : 'green'} />
                        <Card title="Coefficient Source" value={overview.coefficient_source} baseline="default_config" />
                    </div>
                </section>

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                    {/* Sports Section */}
                    <section className="space-y-4">
                        <h2 className="text-xl font-semibold text-white border-b border-gray-700 pb-2">Sports / MLB</h2>
                        {!sports ? (
                            <div className="bg-gray-800/50 rounded-xl p-8 text-center border border-gray-800">
                                <FileJson className="w-8 h-8 text-gray-600 mx-auto mb-2" />
                                <p className="text-gray-500">No sports reports found</p>
                            </div>
                        ) : (
                            <div className="grid grid-cols-2 gap-4">
                                <Card title="Avg Model Prob" value={sports.average_model_prob?.toFixed(4)} />
                                <Card title="Avg Market Prob" value={sports.average_market_prob?.toFixed(4)} />
                                <Card title="Avg Edge Before" value={sports.average_edge_before_execution?.toFixed(4)} />
                                <Card title="Avg Net Edge" value={sports.average_net_edge_after_execution?.toFixed(4)} />
                                <Card title="Avg Exec Cost" value={sports.average_executable_cost?.toFixed(4)} />
                                <Card title="Avg Fee/Share" value={sports.average_fee_per_share?.toFixed(4)} />
                                <Card title="Avg Visible Depth" value={sports.average_visible_depth?.toFixed(2)} />
                                <Card title="Avg Paper Fill Size" value={sports.average_paper_fill_size?.toFixed(2)} />
                            </div>
                        )}
                    </section>

                    {/* Weather Section */}
                    <section className="space-y-4">
                        <h2 className="text-xl font-semibold text-white border-b border-gray-700 pb-2">Weather</h2>
                        <div className="grid grid-cols-2 gap-4">
                            <Card title="Events Processed" value={weather.weather_events_processed} />
                            <Card title="Would-Trade Baskets" value={weather.would_trade_baskets} />
                            <Card title="Paper Fills" value={weather.paper_fills} />
                            <Card title="Vector Failures" value={weather.probability_vector_validation_failures} status={weather.probability_vector_validation_failures > 0 ? 'red' : 'green'} />
                            <Card title="Incomplete Quarantines" value={weather.incomplete_bucket_quarantines} />
                            <Card title="Nowcast Leaks" value={weather.nowcast_impossible_bucket_leaks} status={weather.nowcast_impossible_bucket_leaks > 0 ? 'red' : 'green'} />
                            <Card title="Optimizer Failures" value={weather.optimizer_failures} />
                            <Card title="Avg expected log growth" value={weather.average_delta_expected_log_growth?.toFixed(5)} />
                        </div>
                    </section>

                    {/* Execution Layer */}
                    <section className="space-y-4">
                        <h2 className="text-xl font-semibold text-white border-b border-gray-700 pb-2">Execution Layer</h2>
                        <div className="grid grid-cols-2 gap-4">
                            <Card title="Snapshots Received" value={execution.orderbook_snapshots_received} />
                            <Card title="Stale Books Rejected" value={execution.stale_orderbooks_rejected} baseline="0" status={execution.stale_orderbooks_rejected > 0 ? 'red' : 'green'} />
                            <Card title="Avg Orderbook Age (ms)" value={execution.average_orderbook_age_ms?.toFixed(0)} />
                            <Card title="P95 Orderbook Age (ms)" value={execution.p95_orderbook_age_ms?.toFixed(0)} />
                            <Card title="Price Moved Against" value={execution.price_moved_against_us_on_reprice} />
                            <Card title="Depth Evaporated" value={execution.depth_evaporated_on_reprice} />
                            <Card title="Edge Gone on Reprice" value={execution.edge_gone_after_reprice} />
                            <Card title="Fee Model Unavailable" value={execution.fee_model_unavailable} baseline="0" status={execution.fee_model_unavailable > 0 ? 'red' : 'green'} />
                            <Card title="Partial Fills" value={execution.partial_fill_count} />
                        </div>
                    </section>

                    {/* CLV Tracking */}
                    <section className="space-y-4">
                        <h2 className="text-xl font-semibold text-white border-b border-gray-700 pb-2">CLV Tracking</h2>
                        <div className="grid grid-cols-2 gap-4">
                            <Card title="Records Created" value={clv.clv_records_created} />
                            <Card title="Missing Market Price" value={clv.missing_market_price_count} baseline="0" status={clv.missing_market_price_count > 0 ? 'yellow' : 'green'} />
                            <Card title="15m Coverage" value={`${clv._15m_checkpoint_coverage_pct?.toFixed(1)}%`} baseline="best-effort when scheduler cadence allows" status={clv._15m_checkpoint_coverage_pct < 80 && clv._15m_checkpoints_due > 0 ? 'yellow' : 'green'} />
                            <Card title="1h Coverage" value={`${clv._1h_checkpoint_coverage_pct?.toFixed(1)}%`} baseline="primary GitHub Actions validation checkpoint" status={clv._1h_checkpoint_coverage_pct < 80 && clv._1h_checkpoints_due > 0 ? 'yellow' : 'green'} />
                            <Card title="Median CLV 15m" value={clv.median_clv_15m?.toFixed(4)} />
                            <Card title="Median CLV 1h" value={clv.median_clv_1h?.toFixed(4)} />
                            <Card title="Positive CLV 15m" value={`${(clv.positive_clv_rate_15m * 100).toFixed(1)}%`} baseline="> 50%" />
                            <Card title="Positive CLV 1h" value={`${(clv.positive_clv_rate_1h * 100).toFixed(1)}%`} baseline="> 50%" />
                        </div>
                    </section>
                </div>

                {/* Rejections Table */}
                <section className="space-y-4 pt-4">
                    <h2 className="text-xl font-semibold text-white border-b border-gray-700 pb-2">Rejections & Logs</h2>
                    <div className="bg-gray-800 rounded-xl overflow-hidden border border-gray-700">
                        <table className="w-full text-left text-sm text-gray-300">
                            <thead className="text-xs text-gray-400 bg-gray-900/50 uppercase">
                                <tr>
                                    <th className="px-6 py-3">Reason</th>
                                    <th className="px-6 py-3">Count</th>
                                    <th className="px-6 py-3">%</th>
                                    <th className="px-6 py-3">Example Market</th>
                                    <th className="px-6 py-3">Severity</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rejections.length === 0 && (
                                    <tr><td colSpan={5} className="px-6 py-4 text-center text-gray-500">No rejections found</td></tr>
                                )}
                                {rejections.map((r: any, i: number) => (
                                    <tr key={i} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                                        <td className="px-6 py-4 font-medium text-white">{r.reason}</td>
                                        <td className="px-6 py-4">{r.count}</td>
                                        <td className="px-6 py-4">{r.percentage.toFixed(1)}%</td>
                                        <td className="px-6 py-4 text-gray-400 font-mono text-xs">{r.example_market_id}</td>
                                        <td className="px-6 py-4">
                                            <span className={`px-2 py-1 rounded text-xs font-bold ${
                                                r.severity === 'red' ? 'bg-red-900/50 text-red-400 border border-red-500/30' :
                                                r.severity === 'yellow' ? 'bg-yellow-900/50 text-yellow-400 border border-yellow-500/30' :
                                                'bg-green-900/50 text-green-400 border border-green-500/30'
                                            }`}>
                                                {r.severity.toUpperCase()}
                                            </span>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </section>
                
                {/* Data Consistency Checks */}
                <section className="space-y-4 pt-4 pb-12">
                    <h2 className="text-xl font-semibold text-white border-b border-gray-700 pb-2">Data Consistency Checks</h2>
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {data_consistency.map((c: any, i: number) => (
                            <div key={i} className={`p-4 rounded-xl border ${c.flagged ? 'bg-red-900/20 border-red-500/50' : 'bg-gray-800 border-gray-700'}`}>
                                <div className="flex items-center gap-3">
                                    {c.flagged ? <XCircle className="text-red-500 w-5 h-5" /> : <CheckCircle className="text-green-500 w-5 h-5" />}
                                    <span className={c.flagged ? 'text-red-200' : 'text-gray-300'}>{c.rule}</span>
                                </div>
                            </div>
                        ))}
                    </div>
                </section>

            </div>
        </div>
    );
}
