from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import httpx

from scraper import scrape_consultation
from calculator import calculate_winners, RankedBidder, EXCESSIVE_THRESHOLD, LOW_THRESHOLD

app = FastAPI(
    title="Moroccan Procurement Winner",
    description=(
        "Scrapes marchespublics.gov.ma and ranks bidders per Article 13 of the RC "
        "and Decree n°2-22-431 (08 mars 2023): reference price method."
    ),
    version="2.0.0",
)


class ConsultationRequest(BaseModel):
    url: str


class BidderResult(BaseModel):
    position: int
    name: str
    price: Optional[float]
    distance_to_ref: Optional[float]
    side: str
    admin_status: str
    financial_status: str
    is_eligible: bool
    note: str


class ConsultationResult(BaseModel):
    reference: str
    object: str
    procedure: str
    category: str
    estimated_price: Optional[float]
    estimated_price_currency: str
    reference_price: Optional[float]
    excessive_threshold: Optional[float]
    low_threshold: Optional[float]
    total_bidders: int
    eligible_bidders: int
    winner: Optional[str]
    winner_price: Optional[float]
    top10: list[BidderResult]
    all_rankings: list[BidderResult]


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Moroccan Procurement Winner</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Segoe UI',sans-serif;background:#f0f4f8;color:#1a202c}
    header{background:linear-gradient(135deg,#1a56db,#0e9f6e);color:#fff;padding:2rem;text-align:center}
    header h1{font-size:1.8rem;margin-bottom:.4rem}
    header p{opacity:.85;font-size:.95rem}
    main{max-width:960px;margin:2rem auto;padding:0 1rem}
    .card{background:#fff;border-radius:12px;padding:2rem;box-shadow:0 2px 12px rgba(0,0,0,.08);margin-bottom:1.5rem}
    .input-row{display:flex;gap:.75rem;flex-wrap:wrap}
    input[type=text]{flex:1;min-width:260px;padding:.75rem 1rem;border:2px solid #e2e8f0;border-radius:8px;font-size:1rem;transition:border-color .2s}
    input[type=text]:focus{outline:none;border-color:#1a56db}
    button{padding:.75rem 1.75rem;background:#1a56db;color:#fff;border:none;border-radius:8px;font-size:1rem;cursor:pointer;transition:background .2s}
    button:hover{background:#1648c0}
    button:disabled{background:#a0aec0;cursor:not-allowed}
    #status{margin-top:1rem;font-size:.9rem;min-height:1.2rem}
    #results{display:none}
    .winner-box{background:linear-gradient(135deg,#f0fff4,#e6fffa);border:2px solid #0e9f6e;border-radius:10px;padding:1.25rem 1.5rem;margin-bottom:1.5rem}
    .winner-box h2{color:#0e9f6e;font-size:1.1rem;margin-bottom:.5rem}
    .winner-name{font-size:1.4rem;font-weight:700}
    .winner-sub{color:#4a5568;margin-top:.25rem;font-size:.9rem}
    .ref-box{background:#ebf8ff;border:1px solid #bee3f8;border-radius:8px;padding:1rem 1.25rem;margin-bottom:1rem;font-size:.9rem}
    .ref-box strong{color:#2b6cb0}
    .meta-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:1rem;margin-bottom:1rem}
    .meta-item label{font-size:.72rem;text-transform:uppercase;color:#718096;letter-spacing:.05em}
    .meta-item p{font-weight:600;margin-top:.2rem;color:#2d3748}
    .top5-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:1rem;margin-bottom:.5rem}
    .top5-card{border-radius:10px;padding:1rem;text-align:center;border:2px solid transparent}
    .top5-card.p1{background:#fffbeb;border-color:#f6c90e}
    .top5-card.p2{background:#f0fff4;border-color:#0e9f6e}
    .top5-card.p3{background:#ebf8ff;border-color:#4299e1}
    .top5-card.p4,.top5-card.p5,.top5-card.p6,.top5-card.p7,.top5-card.p8,.top5-card.p9,.top5-card.p10{background:#f7fafc;border-color:#e2e8f0}
    .top5-card .pos{font-size:1.5rem;font-weight:800;color:#4a5568}
    .top5-card.p1 .pos{color:#b7791f}
    .top5-card .cname{font-size:.8rem;font-weight:600;margin:.3rem 0;word-break:break-word}
    .top5-card .cprice{font-size:.85rem;color:#2d3748}
    .top5-card .cdist{font-size:.72rem;color:#718096;margin-top:.2rem}
    .top5-card .cside-below{color:#0e9f6e;font-size:.7rem;font-weight:600}
    .top5-card .cside-above{color:#e53e3e;font-size:.7rem;font-weight:600}
    table{width:100%;border-collapse:collapse;font-size:.85rem}
    th{background:#f7fafc;color:#4a5568;text-transform:uppercase;font-size:.7rem;letter-spacing:.05em;padding:.65rem .75rem;text-align:left;border-bottom:2px solid #e2e8f0}
    td{padding:.6rem .75rem;border-bottom:1px solid #edf2f7;vertical-align:middle}
    tr:hover td{background:#f7fafc}
    .row-winner td{background:#fffff0;font-weight:600}
    .row-2 td{background:#f0fff4}
    .row-3 td{background:#ebf8ff}
    .row-elim td{color:#a0aec0}
    .badge{display:inline-block;padding:.18rem .5rem;border-radius:20px;font-size:.68rem;font-weight:600;white-space:nowrap}
    .bg{background:#c6f6d5;color:#22543d}
    .br{background:#fed7d7;color:#742a2a}
    .by{background:#fefcbf;color:#744210}
    .bgr{background:#e2e8f0;color:#4a5568}
    .side-below{color:#0e9f6e;font-weight:700;font-size:.75rem}
    .side-above{color:#e53e3e;font-weight:700;font-size:.75rem}
    .note-win{color:#0e9f6e;font-weight:700}
    .note-elim{color:#e53e3e;font-size:.78rem}
    details summary{cursor:pointer;padding:.5rem 0;font-weight:600;color:#4a5568;font-size:.9rem}
  </style>
</head>
<body>
<header>
  <h1>&#127950; Moroccan Procurement Winner</h1>
  <p>Reference price method — Decree n°2-22-431 &amp; Article 13 RC</p>
</header>
<main>
  <div class="card">
    <div class="input-row">
      <input type="text" id="url-input"
        placeholder="https://www.marchespublics.gov.ma/?page=entreprise.SuiviConsultation&refConsultation=..."/>
      <button id="btn" onclick="analyze()">Analyze</button>
    </div>
    <div id="status"></div>
  </div>

  <div id="results">
    <div class="winner-box" id="winner-box"></div>

    <div class="card">
      <h2 style="font-size:1.05rem;color:#2d3748;margin-bottom:.75rem">Top 10 Ranked Offers</h2>
      <div class="top5-grid" id="top5-grid"></div>
    </div>

    <div class="card">
      <h2 style="font-size:1.05rem;color:#2d3748;margin-bottom:.75rem">Consultation Details</h2>
      <div class="ref-box" id="ref-box"></div>
      <div class="meta-grid" id="meta-grid"></div>
    </div>

    <div class="card">
      <details open>
        <summary>Full Rankings (all bidders)</summary>
        <div style="overflow-x:auto;margin-top:1rem">
          <table>
            <thead><tr>
              <th>#</th><th>Company</th><th>Admin</th><th>Financial</th>
              <th>Price (MAD)</th><th>Distance to P</th><th>Side</th><th>Note</th>
            </tr></thead>
            <tbody id="table-body"></tbody>
          </table>
        </div>
      </details>
    </div>
  </div>
</main>
<script>
const fmt = n => n==null ? '—' :
  new Intl.NumberFormat('fr-MA',{minimumFractionDigits:2,maximumFractionDigits:2}).format(n);

function badge(s){
  if(!s) return '';
  const l=s.toLowerCase();
  if(l.includes('admissible')) return `<span class="badge bg">${s}</span>`;
  if(l.includes('cart')||l.includes('rejet')||l.includes('ferm'))
    return `<span class="badge br">${s}</span>`;
  if(l.includes('ouverte')) return `<span class="badge by">${s}</span>`;
  return `<span class="badge bgr">${s}</span>`;
}

function sideHtml(side, cls=''){
  if(side==='below') return `<span class="${cls||'side-below'}">&#9660; below P</span>`;
  if(side==='above') return `<span class="${cls||'side-above'}">&#9650; above P</span>`;
  return '—';
}

async function analyze(){
  const url=document.getElementById('url-input').value.trim();
  if(!url){setStatus('Please enter a URL.',true);return;}
  const btn=document.getElementById('btn');
  btn.disabled=true;
  setStatus('Fetching and analyzing…');
  document.getElementById('results').style.display='none';
  try{
    const res=await fetch('/analyze',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})});
    if(!res.ok){const e=await res.json();setStatus('Error: '+(e.detail||res.statusText),true);return;}
    render(await res.json());
    setStatus('');
  }catch(e){setStatus('Network error: '+e.message,true);}
  finally{btn.disabled=false;}
}

function setStatus(m,err=false){
  const el=document.getElementById('status');
  el.textContent=m; el.style.color=err?'#e53e3e':'#4a5568';
}

function render(d){
  // Winner box
  const wb=document.getElementById('winner-box');
  if(d.winner){
    const savings=d.estimated_price?((d.estimated_price-d.winner_price)/d.estimated_price*100).toFixed(1):null;
    wb.innerHTML=`<h2>&#127942; Winner</h2>
      <div class="winner-name">${d.winner}</div>
      <div class="winner-sub">
        Offer: <strong>${fmt(d.winner_price)} ${d.estimated_price_currency}</strong>
        &nbsp;|&nbsp; Reference price: <strong>${fmt(d.reference_price)} MAD</strong>
        &nbsp;|&nbsp; Budget: ${fmt(d.estimated_price)} ${d.estimated_price_currency}
        ${savings?`&nbsp;|&nbsp; <span style="color:#0e9f6e">&#9660; ${savings}% vs budget</span>`:''}
      </div>`;
  } else {
    wb.innerHTML='<h2>No eligible winner found</h2>';
  }

  // Ref price explanation
  document.getElementById('ref-box').innerHTML=`
    <strong>Reference price formula (Art. 13 RC):</strong>
    P = (E + average of valid offers) / 2
    &nbsp;=&nbsp; (${fmt(d.estimated_price)} + avg) / 2
    &nbsp;=&nbsp; <strong>${fmt(d.reference_price)} MAD</strong>
    &nbsp;&nbsp;|&nbsp;&nbsp;
    Excessive threshold: ${fmt(d.excessive_threshold)} MAD (+20%)
    &nbsp;&nbsp;|&nbsp;&nbsp;
    Low threshold: ${fmt(d.low_threshold)} MAD (-25%)
  `;

  // Meta
  document.getElementById('meta-grid').innerHTML=`
    <div class="meta-item"><label>Reference</label><p>${d.reference}</p></div>
    <div class="meta-item"><label>Object</label><p style="font-size:.8rem">${d.object}</p></div>
    <div class="meta-item"><label>Procedure</label><p>${d.procedure}</p></div>
    <div class="meta-item"><label>Category</label><p>${d.category}</p></div>
    <div class="meta-item"><label>Total Bidders</label><p>${d.total_bidders}</p></div>
    <div class="meta-item"><label>Eligible</label><p>${d.eligible_bidders}</p></div>
    <div class="meta-item"><label>Est. Price</label><p>${fmt(d.estimated_price)} ${d.estimated_price_currency}</p></div>
    <div class="meta-item"><label>Ref. Price P</label><p>${fmt(d.reference_price)} MAD</p></div>
  `;

  // Top 10 cards
  const posClass=['p1','p2','p3','p4','p5','p6','p7','p8','p9','p10'];
  const medals=['🥇','🥈','🥉','4','5','6','7','8','9','10'];
  const grid=document.getElementById('top5-grid');
  grid.innerHTML='';
  d.top10.forEach((r,i)=>{
    grid.innerHTML+=`<div class="top5-card ${posClass[i]}">
      <div class="pos">${medals[i]}</div>
      <div class="cname">${r.name}</div>
      <div class="cprice">${fmt(r.price)} MAD</div>
      <div class="cdist">Δ ${fmt(r.distance_to_ref)} MAD</div>
      <div>${r.side==='below'?'<span class="cside-below">▼ below P</span>':'<span class="cside-above">▲ above P</span>'}</div>
    </div>`;
  });

  // Full table
  const tb=document.getElementById('table-body');
  tb.innerHTML='';
  d.all_rankings.forEach(r=>{
    const rc=!r.is_eligible?'row-elim':r.position===1?'row-winner':r.position===2?'row-2':r.position===3?'row-3':'';
    const noteHtml=r.position===1&&r.is_eligible
      ?`<span class="note-win">&#127942; Winner</span>`
      :r.note?`<span class="note-elim">${r.note}</span>`:'';
    tb.innerHTML+=`<tr class="${rc}">
      <td><strong>${r.is_eligible?r.position:'—'}</strong></td>
      <td>${r.name}</td>
      <td>${badge(r.admin_status)}</td>
      <td>${badge(r.financial_status)}</td>
      <td>${fmt(r.price)}</td>
      <td>${fmt(r.distance_to_ref)}</td>
      <td>${r.is_eligible?sideHtml(r.side):'—'}</td>
      <td>${noteHtml}</td>
    </tr>`;
  });

  document.getElementById('results').style.display='block';
  document.getElementById('results').scrollIntoView({behavior:'smooth'});
}

document.getElementById('url-input').addEventListener('keydown',e=>{if(e.key==='Enter')analyze();});
</script>
</body>
</html>
"""


@app.post("/analyze", response_model=ConsultationResult)
async def analyze(request: ConsultationRequest):
    try:
        data = scrape_consultation(request.url)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch page: {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scraping error: {str(e)}")

    if not data.bidders:
        raise HTTPException(
            status_code=422,
            detail="No bidder data found. Make sure the URL points to a 'SuiviConsultation' results page.",
        )

    rankings, method, reference_price = calculate_winners(data)

    winner = next((r for r in rankings if r.position == 1 and r.is_eligible), None)
    eligible_rankings = [r for r in rankings if r.is_eligible]
    top5 = eligible_rankings[:10]

    excessive_thresh = data.estimated_price * EXCESSIVE_THRESHOLD if data.estimated_price else None
    low_thresh = data.estimated_price * LOW_THRESHOLD if data.estimated_price else None

    def to_result(r: RankedBidder) -> BidderResult:
        return BidderResult(
            position=r.position,
            name=r.name,
            price=r.price,
            distance_to_ref=r.distance_to_ref,
            side=r.side,
            admin_status=r.admin_status,
            financial_status=r.financial_status,
            is_eligible=r.is_eligible,
            note=r.note,
        )

    return ConsultationResult(
        reference=data.reference,
        object=data.object,
        procedure=data.procedure,
        category=data.category,
        estimated_price=data.estimated_price,
        estimated_price_currency=data.estimated_price_currency,
        reference_price=round(reference_price, 2) if reference_price else None,
        excessive_threshold=round(excessive_thresh, 2) if excessive_thresh else None,
        low_threshold=round(low_thresh, 2) if low_thresh else None,
        total_bidders=len(data.bidders),
        eligible_bidders=len(eligible_rankings),
        winner=winner.name if winner else None,
        winner_price=winner.price if winner else None,
        top10=[to_result(r) for r in top5],
        all_rankings=[to_result(r) for r in rankings],
    )
