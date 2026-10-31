"use client";
import { useCallback, useEffect, useState } from "react";
import { api, type Review, type Finding, ApiError } from "@/lib/api";
import { Notice, PageHeader, Panel } from "@/components/ui";
const levels: Finding["severity"][] = ["high", "medium", "low"];
export function ReviewFinder({ id }: { id: string }) {
 const [review,setReview]=useState<Review|null>(null),[error,setError]=useState(""),[filter,setFilter]=useState("all"),[busy,setBusy]=useState<string|null>(null);
 const load=useCallback(async()=>{try{const r=await api.review(id);setReview(r);setError("");return r.status;}catch(e){setError(e instanceof ApiError?e.message:"Could not load review.");return null;}},[id]);
 useEffect(()=>{let mounted=true,timer:ReturnType<typeof setTimeout>;const poll=async()=>{const status=await load();if(mounted&&["queued","running"].includes(status||""))timer=setTimeout(poll,2500);};void poll();return()=>{mounted=false;clearTimeout(timer);};},[load]);
 async function feedback(f:Finding,value:"helpful"|"false_positive"){setBusy(f.id);try{await api.feedback(f.id,value);setReview(r=>r?{...r,findings:r.findings.map(x=>x.id===f.id?{...x,feedback:value}:x)}:r);}catch(e){setError(e instanceof ApiError?e.message:"Feedback could not be saved.");}finally{setBusy(null);}}
 if(error&&!review)return <Notice kind="error">{error}</Notice>;if(!review)return <Notice kind="loading">Loading review…</Notice>;
 const findings=filter==="all"?review.findings:review.findings.filter(f=>f.severity===filter);
 return <><PageHeader eyebrow="REVIEW DETAIL" title={`Review ${review.id.slice(0,8)}`} subtitle={`Commit ${review.commit_sha.slice(0,10)} · ${review.status.replaceAll("_"," ")}`} action={<span className={`status ${review.status}`}>{review.status}</span>}/>
 {error&&<Notice kind="error">{error}</Notice>}<div className="metrics"><div><small>FINDINGS</small><strong>{review.findings.length}</strong></div><div><small>MODEL</small><strong>{review.model_name||"—"}</strong></div><div><small>RUNTIME</small><strong>{review.duration_ms==null?"—":`${(review.duration_ms/1000).toFixed(1)}s`}</strong></div><div><small>TOKENS</small><strong>{review.input_tokens==null?"—":review.input_tokens+ (review.output_tokens||0)}</strong></div></div>
 <Panel title="Findings" aside={<select aria-label="Filter severity" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">All severities</option>{levels.map(l=><option key={l}>{l}</option>)}</select>}>
 {findings.length===0?<Notice kind="empty">{review.findings.length?"No findings match this filter.":"No findings. This review found nothing to report."}</Notice>:levels.filter(level=>findings.some(f=>f.severity===level)).map(level=><section key={level} className="severity-group"><h3><i className={`dot ${level}`}/>{level}<span>{findings.filter(f=>f.severity===level).length}</span></h3>{findings.filter(f=>f.severity===level).map(f=><article className="finding" key={f.id}><div className="finding-top"><b>{f.title}</b><code>{f.file_path}{f.line_number?`:${f.line_number}`:""}</code></div><p>{f.explanation}</p>{f.suggested_fix&&<p><b>Suggested fix:</b> {f.suggested_fix}</p>}<div className="feedback"><span>Was this useful?</span><button disabled={busy===f.id} aria-pressed={f.feedback==="helpful"} onClick={()=>feedback(f,"helpful")}>Helpful</button><button disabled={busy===f.id} aria-pressed={f.feedback==="false_positive"} onClick={()=>feedback(f,"false_positive")}>False positive</button></div></article>)}</section>)}
 </Panel></>;
}
