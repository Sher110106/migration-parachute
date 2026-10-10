from __future__ import annotations
import json, os, subprocess, textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'demo'
FRAMES = OUT / 'frames'
FRAMES.mkdir(parents=True, exist_ok=True)
for p in FRAMES.glob('*.png'):
    p.unlink()

def font(size, bold=False):
    paths = [
        '/System/Library/Fonts/SFNS.ttf',
        '/System/Library/Fonts/SFNSDisplay.ttf',
        '/System/Library/Fonts/HelveticaNeue.ttc',
    ]
    if bold:
        paths = ['/System/Library/Fonts/SFNSDisplay-Bold.otf'] + paths
    for p in paths:
        if Path(p).exists():
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

F_BODY, F_SMALL, F_TITLE, F_BIG = font(28), font(22), font(54, True), font(92, True)
BG = '#F7F6F3'; INK = '#111111'; MUTED = '#787774'; BORDER = '#DAD8D2'; RED = '#9F2F2D'; REDBG = '#FDEBEC'; GREEN = '#346538'; GREENBG = '#EDF3EC'; YELLOW = '#956400'; YELLOWBG = '#FBF3DB'; BLUE = '#1F6C9F'; BLUEBG = '#E1F3FE'

def wrap(draw, text, x, y, width, f=F_BODY, fill=INK, gap=10):
    words = text.split(); lines=[]; line=''
    for w in words:
        t = (line+' '+w).strip()
        if draw.textbbox((0,0),t,font=f)[2] <= width: line=t
        else: lines.append(line); line=w
    if line: lines.append(line)
    for l in lines:
        draw.text((x,y),l,font=f,fill=fill); y += f.size+gap
    return y

def base(kicker, title, subtitle=''):
    im=Image.new('RGB',(1920,1080),BG); d=ImageDraw.Draw(im)
    d.text((92,70),kicker.upper(),font=F_SMALL,fill=MUTED)
    d.text((92,120),title,font=F_TITLE,fill=INK)
    if subtitle: wrap(d,subtitle,96,205,1000,F_BODY,MUTED)
    d.line((92,300,1828,300),fill=BORDER,width=2)
    d.text((92,1010),'Migration Parachute  /  neatHack 2026  /  fictional local fixture',font=F_SMALL,fill=MUTED)
    return im,d

def box(d, xy, title, value, fill=BG, accent=INK, body=''):
    x,y,w,h=xy; d.rounded_rectangle((x,y,x+w,y+h),radius=12,fill=fill,outline=BORDER,width=2)
    d.text((x+28,y+22),title,font=F_SMALL,fill=MUTED)
    d.text((x+28,y+66),value,font=font(42,True),fill=accent)
    if body: wrap(d,body,x+28,y+h-84,w-56,F_SMALL,MUTED,5)

def save(im, i):
    p=FRAMES/f'{i:03d}.png'; im.save(p); return p

before=json.loads((ROOT/'evidence/neatlogs-before-naive-v0.json').read_text())[0]
after=json.loads((ROOT/'evidence/neatlogs-after-adaptive-v1.json').read_text())[0]
frames=[]
# 0 hook
im,d=base('The problem','A correct migration can still take down a service.','Migration Parachute runs the risky part in short, recoverable steps.');
box(d,(96,380,520,280),'Fixture','2,000 customers',BG,BLUE,'10,000 orders on local Postgres 15')
box(d,(660,380,520,280),'Goal','email → customer_id',BG,INK,'Expand, backfill, verify, then gate the destructive contract')
box(d,(1224,380,600,280),'Safety rule','Proof before contract',GREENBG,GREEN,'No drop, rename, or NOT NULL change without parity evidence and owner approval')
frames.append((save(im,0),10))
# 1 baseline
im,d=base('Baseline run','The naive policy hits the lock and stops.','A second session holds the rows the one-transaction update needs.');
box(d,(96,375,420,250),'Outcome','PAUSED',REDBG,RED,'The run never claims success')
box(d,(560,375,420,250),'Retries','6',REDBG,RED,'Same 2,000-row batch each time')
box(d,(1024,375,360,250),'Rows kept','4,000',REDBG,RED,'Only earlier chunks remain')
box(d,(1428,375,396,250),'Trace','0b57a2f5…',BG,INK,'neatlogs before trace')
frames.append((save(im,1),20))
# 2 adaptive recovery
im,d=base('Adaptive recovery','The agent changes its plan when the lock is real.','It rolls back only the failed chunk, records the blocker, shrinks the batch, and resumes from the saved cursor.');
steps=[('01','Observe','SQLSTATE 55P03\nblocker age recorded',BLUEBG,BLUE),('02','Recover','2,000 → 500 → 250\nbackoff grows',YELLOWBG,YELLOW),('03','Resume','same cursor\nno duplicate writes',BG,INK),('04','Verify','checksum parity\n0 mismatches',GREENBG,GREEN)]
for i,(num,t,b,fill,accent) in enumerate(steps):
 x=96+i*440; d.rounded_rectangle((x,390,x+380,700),radius=12,fill=fill,outline=BORDER,width=2); d.text((x+26,420),num,font=F_SMALL,fill=MUTED); d.text((x+26,470),t,font=font(34,True),fill=accent); wrap(d,b,x+26,535,320,F_BODY,INK,8)
 if i<3: d.line((x+380,545,x+430,545),fill=BORDER,width=5)
box(d,(96,780,520,130),'Lock wait','1.84 s',BG,INK,'Adaptive trace')
box(d,(660,780,520,130),'Retries','4',BG,INK,'bounded recovery decisions')
box(d,(1224,780,600,130),'Rows verified','10,000 / 10,000',GREENBG,GREEN,'source checksum = target checksum')
frames.append((save(im,2),45))
# 3 verification gate
im,d=base('Deterministic verifier','The model does not get to declare success.','A consistent snapshot checks the data, the schema, and the side-effect ledger.');
checks=[('Schema fingerprint','matched',GREEN),('Source checksum','4b267091…',GREEN),('Target checksum','4b267091…',GREEN),('Mismatches','0',GREEN),('Approval gate','CONTRACT_READY',YELLOW)]
for i,(a,b,c) in enumerate(checks):
 y=370+i*90; d.rounded_rectangle((96,y,1824,y+66),radius=8,fill=GREENBG if c==GREEN else YELLOWBG,outline=BORDER,width=2); d.text((130,y+18),a,font=F_BODY,fill=INK); d.text((1120,y+18),b,font=F_BODY,fill=c)
wrap(d,'The destructive contract is prepared, but it is still waiting for an owner approval bound to the exact SQL hash and proof hash.',96,850,1450,F_BODY,MUTED,6)
frames.append((save(im,3),18))
# 4 neatlogs
im,d=base('neatlogs','Before and after are visible in the trace tree.','The same fixture and the same fault make the reliability improvement measurable.');
box(d,(96,390,760,300),'Before / naive','6 retries · 2.284 s lock wait',REDBG,RED,'PAUSED_LOCK_BUDGET  |  trace 0b57a2f5…')
box(d,(1064,390,760,300),'After / adaptive','4 retries · 1.840 s lock wait',GREENBG,GREEN,'CONTRACT_READY  |  trace c583c2fb…')
d.line((860,540,1060,540),fill=BORDER,width=6); d.polygon([(1060,540),(1025,520),(1025,560)],fill=BORDER)
wrap(d,'WORKFLOW → AGENT → TOOL → GUARDRAIL → EVALUATOR. Every span carries run, phase, attempt, lock, and proof metadata.',96,820,1500,F_BODY,MUTED,6)
frames.append((save(im,4),18))
# 5 Entire
im,d=base('Entire','The fix has a development history, not just a diff.','Entire’s public evidence is the graph and checkpoint metadata. Raw transcripts stay private.');
box(d,(96,390,760,280),'Naive commit','ed9edbe',BG,INK,'Initial policy: same batch, fixed backoff')
box(d,(1064,390,760,280),'Fix commit','5e05e92',GREENBG,GREEN,'Adaptive batch, blocker inspection, seeded backoff')
d.line((856,530,1060,530),fill=BORDER,width=6); d.polygon([(1060,530),(1025,510),(1025,550)],fill=BORDER)
wrap(d,'Checkpoints and entire-graph output are committed in evidence/entire/. The repo does not publish private session transcripts.',96,820,1550,F_BODY,MUTED,6)
frames.append((save(im,5),13))
# 6 CFO
im,d=base('cfo.ai','A business model tied to measured run costs.','Scenario drivers are editable assumptions; the repository independently recomputes the page outputs.');
rows=[('Base','36.7% margin','month 9 break-even','$84,022 cash at month 24',BLUEBG,BLUE),('Upside','87.5% margin','month 1 break-even','$3.32M cash at month 24',GREENBG,GREEN),('Downside','-153.5% margin','no break-even','cash runs out in month 10',REDBG,RED)]
for i,(a,b,c,e,fill,accent) in enumerate(rows):
 y=370+i*145; d.rounded_rectangle((96,y,1824,y+110),radius=10,fill=fill,outline=BORDER,width=2); d.text((130,y+22),a,font=font(30,True),fill=accent); d.text((500,y+26),b,font=F_BODY,fill=INK); d.text((920,y+26),c,font=F_BODY,fill=INK); d.text((1360,y+26),e,font=F_BODY,fill=INK)
wrap(d,'The page is account-scoped at the moment. The local checker matches the cfo.ai values and is committed as evidence.',96,850,1500,F_BODY,MUTED,6)
frames.append((save(im,6),14))
# 7 close
im,d=base('Final result','12 adversarial replays. Zero false successes.','Migration Parachute is a local, fictional fixture. The contract is always owner-gated.');
box(d,(96,390,500,260),'Replay suite','12 / 12',GREENBG,GREEN,'expected policy behavior')
box(d,(660,390,500,260),'False success','0',GREENBG,GREEN,'no data-loss claims')
box(d,(1224,390,600,260),'Destructive steps','0 without approval',GREENBG,GREEN,'verified by the event log')
wrap(d,'Built for neatHack 2026 · github.com/Sher110106/migration-parachute',96,820,1500,F_BODY,MUTED,6)
frames.append((save(im,7),12))

# concat list for ffmpeg
concat=OUT/'frames.txt'
with concat.open('w') as f:
  for p,dur in frames:
    f.write(f"file '{p}'\nduration {dur}\n")
  f.write(f"file '{frames[-1][0]}'\n")
# voiceover
script='''A correct database migration can still take down a service. Migration Parachute runs the risky part in short, recoverable steps. The demo uses a fictional local fixture with two thousand customers and ten thousand orders. First, the naive policy uses one transaction. A second session holds the rows it needs. The lock timeout rolls the transaction back, and the run pauses without claiming success. Now the adaptive agent observes the SQL state and the blocker age. It rolls back only the failed chunk, shrinks the batch, increases the backoff, and resumes from the saved cursor. Every action comes from a bounded policy. The planner cannot write arbitrary SQL or approve a destructive contract. The verifier checks schema identity, source and target checksums, nulls, mismatches, and the side effect ledger. The adaptive run reaches contract ready with ten thousand rows verified and zero mismatches. neatlogs shows the before and after trace trees. The naive trace used six retries and paused. The adaptive trace used four retries and finished with parity proven. Entire records the development provenance of the reliability fix through checkpoints and graph output. Raw transcripts stay private because the submission asks for checkpoints or graph evidence, not transcript bytes. cfo dot ai models price, run volume, database cost, support, margin, break even, and runway under base, upside, and downside scenarios. The final result is twelve adversarial replay cases, zero false successes, zero destructive statements without approval, and an owner gated contract. This demo uses a local fixture and does not touch production data.'''
(Path('/tmp/migration-parachute-demo.txt')).write_text(script)
subprocess.run(['say','-v','Samantha','-r','185','-o','/tmp/migration-parachute-demo.aiff',script],check=True)
video=OUT/'migration-parachute-demo.mp4'
subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',str(concat),'-i','/tmp/migration-parachute-demo.aiff','-t','150','-r','30','-pix_fmt','yuv420p','-c:v','libx264','-crf','23','-preset','medium','-c:a','aac','-b:a','128k',str(video)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(video, video.stat().st_size)
