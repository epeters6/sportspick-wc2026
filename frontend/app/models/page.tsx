"use client";

import { useState, useEffect } from "react";
import ModelOverviewCard from "@/components/ModelOverviewCard";
import ReadinessChecklist from "@/components/ReadinessChecklist";

export default function ModelsPage() {
  const [overview, setOverview] = useState<any[]>([]);
  const [readiness, setReadiness] = useState<any>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function fetchData() {
      try {
        const [overviewRes, readinessRes] = await Promise.all([
          fetch("http://localhost:8000/models/overview"),
          fetch("http://localhost:8000/models/readiness")
        ]);
        const overviewData = await overviewRes.json();
        const readinessData = await readinessRes.json();
        
        setOverview(overviewData);
        setReadiness(readinessData);
      } catch (e) {
        console.error("Failed to fetch models data", e);
      } finally {
        setLoading(false);
      }
    }
    fetchData();
  }, []);

  return (
    <div className="space-y-10 pb-12 text-slate-200 font-sans">
      <div className="flex flex-col md:flex-row md:items-end justify-between gap-4 border-b border-gray-800/50 pb-6">
        <div>
          <h1 className="text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-indigo-400 via-purple-400 to-pink-400 tracking-tight flex items-center gap-3">
            Model Intelligence Center
          </h1>
          <p className="text-gray-400 text-sm mt-2 max-w-2xl">
            Track learning progress, calibration, and live readiness across all autonomous agents.
          </p>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="w-8 h-8 border-4 border-indigo-500 border-t-transparent rounded-full animate-spin"></div>
        </div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-8">
          {overview.map((model) => (
            <div key={model.id} className="flex flex-col gap-6 bg-[#131B2F] border border-slate-800 rounded-3xl p-6 shadow-2xl relative overflow-hidden">
              {/* Subtle background glow based on readiness */}
              <div className={`absolute -top-32 -right-32 w-64 h-64 rounded-full blur-3xl opacity-20 ${readiness[model.id]?.ready ? 'bg-emerald-500' : 'bg-indigo-500'}`}></div>
              
              <ModelOverviewCard model={model} score={readiness[model.id]?.score || 0} />
              
              <div className="h-px w-full bg-gradient-to-r from-transparent via-slate-800 to-transparent"></div>
              
              <ReadinessChecklist criteria={readiness[model.id]?.criteria} ready={readiness[model.id]?.ready} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
