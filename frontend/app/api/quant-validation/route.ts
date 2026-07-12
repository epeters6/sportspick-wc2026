import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

function readJsonl(filePath: string) {
    if (!fs.existsSync(filePath)) return [];
    const content = fs.readFileSync(filePath, 'utf-8');
    return content.split('\n').filter(line => line.trim()).map(line => {
        try {
            return JSON.parse(line);
        } catch {
            return null;
        }
    }).filter(x => x);
}

function getLatestReport(dirPath: string) {
    if (!fs.existsSync(dirPath)) return null;
    const files = fs.readdirSync(dirPath).filter(f => f.endsWith('_report.json'));
    if (files.length === 0) return null;
    files.sort(); // YYYY-MM-DD will sort correctly
    const latest = files[files.length - 1];
    const content = fs.readFileSync(path.join(dirPath, latest), 'utf-8');
    try {
        return { filename: latest, data: JSON.parse(content), mtime: fs.statSync(path.join(dirPath, latest)).mtime.toISOString() };
    } catch {
        return null;
    }
}

export async function GET() {
    const rootDir = 'C:/Users/eepet/Scraper';
    
    const sportsDecisions = readJsonl(path.join(rootDir, 'sports_shadow_decisions.jsonl'));
    const sportsFills = readJsonl(path.join(rootDir, 'sports_paper_fills.jsonl'));
    const sportsClv = readJsonl(path.join(rootDir, 'sports_clv_tracking.jsonl'));
    
    const weatherDecisions = readJsonl(path.join(rootDir, 'weather_shadow_decisions.jsonl'));
    const weatherFills = readJsonl(path.join(rootDir, 'paper_fills.jsonl'));
    const weatherClv = readJsonl(path.join(rootDir, 'clv_tracking.jsonl'));
    
    const latestSportsReport = getLatestReport(path.join(rootDir, 'reports', 'sports_shadow'));
    
    // Combine logs
    const allDecisions = [...sportsDecisions, ...weatherDecisions];
    const allFills = [...sportsFills, ...weatherFills];
    const allClv = [...sportsClv, ...weatherClv];
    const orderbookSnapshots = readJsonl(path.join(rootDir, 'orderbook_snapshots.jsonl'));
    
    let syncStatus = {
        last_status: 'Unknown',
        last_started_at: new Date().toISOString(),
        last_duration_seconds: 0,
        last_exit_code: null,
        last_error: null,
        mode: 'shadow'
    };
    try {
        const syncStatusStr = fs.readFileSync(path.join(rootDir, 'sync_status.json'), 'utf-8');
        syncStatus = JSON.parse(syncStatusStr);
    } catch (e) {}
    
    // Derived Metrics
    let liveOrdersSubmitted = 0; // we don't have real live logs here but we can check if mode was somehow live
    let liveOrderAttemptsBlocked = 0;
    
    let totalPredictions = allDecisions.length;
    let totalRejections = 0;
    let totalWouldTrade = 0;
    
    let missingTimestamps = 0;
    let assumedTimestamps = 0;
    let staleOrderBooksRejected = 0;
    
    let clv15mVals: number[] = [];
    let clv1hVals: number[] = [];
    
    let calibrationStatusSet = new Set<string>();
    let coefficientSourceSet = new Set<string>();
    
    let rejectionReasons: Record<string, {count: number, example: string, first_seen: string, last_seen: string}> = {};
    
    let orderbookAges: number[] = [];
    let missingReceivedTs = 0;
    let missingOrderbookTs = 0;
    let depthEvaporated = 0;
    let priceMovedAgainstUs = 0;
    let edgeGoneAfterReprice = 0;
    let effectiveCostNotTradable = 0;
    let feeModelUnavailable = 0;
    let staticFeeFallback = 0;
    let feesPerShare = [];
    let visibleDepths = [];
    let slippageBuffers = [];
    let partialFillCount = 0;
    
    // Weather specific
    let weatherEventsProcessed = weatherDecisions.length;
    let weatherWouldTrade = 0;
    let weatherPaperFills = weatherFills.length;
    let nowcastLeak = 0;
    let probVectorFailures = 0;
    let incompleteQuarantines = 0;
    let optimizerFailures = 0;
    let roundingInvalidated = 0;
    let deltaExpectedLogGrowth = [];
    
    // CLV specific
    let checkpoints15mDue = 0;
    let checkpoints15mCompleted = 0;
    let checkpoints1hDue = 0;
    let checkpoints1hCompleted = 0;
    let missingMarketPriceCount = 0;

    for (const d of allDecisions) {
        const r = d.rejection_reason;
        if (r) {
            totalRejections++;
            if (!rejectionReasons[r]) {
                rejectionReasons[r] = { count: 0, example: d.market_id || d.event_id || 'unknown', first_seen: d.timestamp || new Date().toISOString(), last_seen: d.timestamp || new Date().toISOString() };
            }
            rejectionReasons[r].count++;
            rejectionReasons[r].last_seen = d.timestamp || new Date().toISOString();
            
            if (r === 'STALE_ORDERBOOK') staleOrderBooksRejected++;
            if (r === 'ORDERBOOK_TIMESTAMP_ASSUMED_FOR_SHADOW') assumedTimestamps++;
            if (r === 'DEPTH_EVAPORATED') depthEvaporated++;
            if (r === 'PRICE_MOVED_AGAINST_US') priceMovedAgainstUs++;
            if (r === 'EDGE_GONE_AFTER_REPRICE') edgeGoneAfterReprice++;
            if (r === 'EFFECTIVE_COST_NOT_TRADABLE' || r === 'EFFECTIVE_COST_GTE_1') effectiveCostNotTradable++;
            if (r === 'FEE_MODEL_UNAVAILABLE') feeModelUnavailable++;
            if (r === 'OPTIMIZER_FAILED') optimizerFailures++;
            if (r === 'ROUNDING_INVALIDATED_TRADE') roundingInvalidated++;
            if (r === 'PROBABILITY_VECTOR_INVALID') probVectorFailures++;
            if (r === 'INCOMPLETE_BUCKET_QUARANTINE') incompleteQuarantines++;
            if (r === 'NOWCAST_IMPOSSIBLE_BUCKET_LEAK') nowcastLeak++;
            
            if (r === 'UNCALIBRATED_MODEL_LIVE_BLOCK') liveOrderAttemptsBlocked++;
        } else {
            totalWouldTrade++;
            if (d.mode === 'live') liveOrdersSubmitted++;
        }
        
        if (d.calibration_status) calibrationStatusSet.add(d.calibration_status);
        if (d.coefficient_source) coefficientSourceSet.add(d.coefficient_source);
        
        if (!d.received_timestamp) {
            missingTimestamps++;
            missingReceivedTs++;
        }
        if (!d.orderbook_timestamp) {
            // will be captured from orderbook snapshots instead, but keep for fallback
        }
        
        if (d.fee_per_share !== undefined && d.fee_per_share !== null) feesPerShare.push(d.fee_per_share);
        if (d.visible_depth !== undefined && d.visible_depth !== null) visibleDepths.push(d.visible_depth);
        if (d.slippage_buffer !== undefined && d.slippage_buffer !== null) slippageBuffers.push(d.slippage_buffer);
        if (d.delta_expected_log_growth) deltaExpectedLogGrowth.push(d.delta_expected_log_growth);
        
        if (d.static_fee_fallback) staticFeeFallback++;
    }
    
    for (const d of weatherDecisions) {
        if (!d.rejection_reason) weatherWouldTrade++;
    }

    let fillRate = allFills.length / (totalWouldTrade || 1);
    
    for (const f of allFills) {
        if (f.is_partial) partialFillCount++;
    }
    
    for (const ob of orderbookSnapshots) {
        if (ob.missing_received_timestamp) missingReceivedTs++;
        if (ob.missing_orderbook_timestamp) missingOrderbookTs++;
        if (ob.age_ms !== undefined) orderbookAges.push(ob.age_ms);
    }
    
    for (const c of allClv) {
        if (c.price_after_15m !== undefined && c.price_after_15m !== null) {
            checkpoints15mCompleted++;
            clv15mVals.push(c.price_after_15m - (c.entry_price || 0));
        } else if (new Date(c.checkpoint_15m_time).getTime() < Date.now()) {
            checkpoints15mDue++;
        }
        
        if (c.price_after_1h !== undefined && c.price_after_1h !== null) {
            checkpoints1hCompleted++;
            clv1hVals.push(c.price_after_1h - (c.entry_price || 0));
        } else if (new Date(c.checkpoint_1h_time).getTime() < Date.now()) {
            checkpoints1hDue++;
        }
        
        if (c.missing_market_price) missingMarketPriceCount++;
    }
    
    const avg = (arr: number[]) => arr.length ? arr.reduce((a,b)=>a+b,0)/arr.length : 0;
    const median = (arr: number[]) => {
        if (!arr.length) return 0;
        const s = [...arr].sort((a,b)=>a-b);
        return s[Math.floor(s.length/2)];
    };
    
    const avg15m = avg(clv15mVals);
    const avg1h = avg(clv1hVals);
    
    const p95_age = orderbookAges.length ? [...orderbookAges].sort((a,b)=>a-b)[Math.floor(orderbookAges.length * 0.95)] : 0;

    let calibStatusStr = Array.from(calibrationStatusSet).join(', ') || 'unknown';
    let coefSrcStr = Array.from(coefficientSourceSet).join(', ') || 'unknown';
    
    // Severity classification
    const rejectionsList = Object.keys(rejectionReasons).map(r => {
        let severity = 'green';
        if (['NOWCAST_IMPOSSIBLE_BUCKET_LEAK', 'UNCALIBRATED_MODEL_LIVE_BLOCK', 'MISSING_ORDERBOOK_TIMESTAMP', 'STALE_ORDERBOOK'].includes(r)) severity = 'red';
        else if (['KALSHI_SPORTS_MAPPING_NOT_IMPLEMENTED', 'SNAPSHOT_AFTER_EVENT_START', 'AMBIGUOUS_TEAM_MARKET_MATCH', 'UNSUPPORTED_MARKET_TYPE', 'DEPTH_EVAPORATED', 'PRICE_MOVED_AGAINST_US', 'EDGE_GONE_AFTER_REPRICE'].includes(r)) severity = 'yellow';
        
        return {
            reason: r,
            count: rejectionReasons[r].count,
            percentage: (rejectionReasons[r].count / totalRejections) * 100,
            example_market_id: rejectionReasons[r].example,
            first_seen: rejectionReasons[r].first_seen,
            last_seen: rejectionReasons[r].last_seen,
            severity
        };
    }).sort((a,b) => b.count - a.count);

    const dataConsistency = [
        { rule: "paper_fills_without_matching_decision", flagged: allFills.length > allDecisions.filter(d => !d.rejection_reason).length },
        { rule: "paper_fills_from_rejected_predictions", flagged: allFills.some(f => allDecisions.some(d => d.rejection_reason && d.market_id === f.market_id && d.outcome_id === f.outcome_id)) },
        { rule: "clv_records_without_matching_fill", flagged: allClv.some(c => !allFills.some(f => f.market_id === c.market_id && f.outcome_id === c.outcome_id)) },
        { rule: "would_trade_without_sized_order", flagged: allDecisions.some(d => !d.rejection_reason && (!d.sized_order || !d.sized_order.target_shares)) },
        { rule: "fill_missing_received_timestamp", flagged: allFills.some(f => f.received_timestamp === undefined && f.orderbook_timestamp === undefined) }, // Assuming they might not have it attached, but they should if we tracked it in fill
        { rule: "fill_missing_executable_cost", flagged: allFills.some(f => f.simulated_fill_price === undefined) },
        { rule: "missing_calibration_status", flagged: allDecisions.some(d => !d.calibration_status) },
        { rule: "missing_coefficient_source", flagged: allDecisions.some(d => !d.coefficient_source) }
    ];
    
    return NextResponse.json({
        overview: {
            last_sync_status: syncStatus.last_status,
            last_sync_time: syncStatus.last_started_at,
            current_mode: syncStatus.mode,
            live_orders_submitted: liveOrdersSubmitted,
            live_order_attempts_blocked: liveOrderAttemptsBlocked,
            total_predictions: totalPredictions,
            total_rejections: totalRejections,
            total_would_trade: totalWouldTrade,
            total_paper_fills: allFills.length,
            missing_timestamps: missingTimestamps,
            assumed_timestamps: assumedTimestamps,
            stale_order_books_rejected: staleOrderBooksRejected,
            average_15m_clv: avg15m,
            average_1h_clv: avg1h,
            calibration_status: calibStatusStr,
            coefficient_source: coefSrcStr
        },
        sports: latestSportsReport ? latestSportsReport.data : null,
        weather: {
            weather_events_processed: weatherEventsProcessed,
            would_trade_baskets: weatherWouldTrade,
            paper_fills: weatherPaperFills,
            probability_vector_validation_failures: probVectorFailures,
            incomplete_bucket_quarantines: incompleteQuarantines,
            nowcast_impossible_bucket_leaks: nowcastLeak,
            effective_cost_not_tradable: effectiveCostNotTradable,
            optimizer_failures: optimizerFailures,
            rounding_invalidated_trades: roundingInvalidated,
            average_delta_expected_log_growth: avg(deltaExpectedLogGrowth),
            average_clv_15m: avg(clv15mVals.slice(-weatherClv.length)),
            average_clv_1h: avg(clv1hVals.slice(-weatherClv.length))
        },
        execution: {
            orderbook_snapshots_received: orderbookSnapshots.length,
            missing_received_timestamps: missingReceivedTs,
            missing_orderbook_timestamps: missingOrderbookTs,
            assumed_fresh_snapshots: assumedTimestamps,
            stale_orderbooks_rejected: staleOrderBooksRejected,
            average_orderbook_age_ms: avg(orderbookAges),
            p95_orderbook_age_ms: p95_age,
            price_moved_against_us_on_reprice: priceMovedAgainstUs,
            depth_evaporated_on_reprice: depthEvaporated,
            edge_gone_after_reprice: edgeGoneAfterReprice,
            effective_cost_gte_1_rejected: effectiveCostNotTradable,
            fee_model_unavailable: feeModelUnavailable,
            static_fee_fallback_count: staticFeeFallback,
            average_fee_per_share: avg(feesPerShare),
            average_slippage_buffer: avg(slippageBuffers),
            average_visible_depth: avg(visibleDepths),
            partial_fill_count: partialFillCount,
            fill_rate: fillRate
        },
        clv: {
            clv_records_created: allClv.length,
            _15m_checkpoints_due: checkpoints15mDue,
            _15m_checkpoints_completed: checkpoints15mCompleted,
            _1h_checkpoints_due: checkpoints1hDue,
            _1h_checkpoints_completed: checkpoints1hCompleted,
            _15m_checkpoint_coverage_pct: checkpoints15mDue > 0 ? (checkpoints15mCompleted / checkpoints15mDue) * 100 : (checkpoints15mCompleted > 0 ? 100 : 0),
            _1h_checkpoint_coverage_pct: checkpoints1hDue > 0 ? (checkpoints1hCompleted / checkpoints1hDue) * 100 : (checkpoints1hCompleted > 0 ? 100 : 0),
            average_clv_15m: avg15m,
            median_clv_15m: median(clv15mVals),
            average_clv_1h: avg1h,
            median_clv_1h: median(clv1hVals),
            positive_clv_rate_15m: clv15mVals.length ? clv15mVals.filter(x => x > 0).length / clv15mVals.length : 0,
            positive_clv_rate_1h: clv1hVals.length ? clv1hVals.filter(x => x > 0).length / clv1hVals.length : 0,
            missing_market_price_count: missingMarketPriceCount
        },
        rejections: rejectionsList,
        data_consistency: dataConsistency
    });
}
