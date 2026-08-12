"""Local returned-HD analysis worker for human profile review."""
from __future__ import annotations
import base64,hashlib,hmac,json,os,re,time,urllib.error,urllib.request
from typing import Any,Callable,Mapping,Optional
from .catalog import SupabaseCatalog
from .gate1b_worker import DEFAULT_MODEL,OPENAI_ENDPOINT,ModelUnavailable
MODEL_NAME="OpenAI-GPT-4o-mini-Vision-HD"
MODEL_VERSION="gpt-4o-mini-2024-07-18@hd-instances-2026-08-12.1"
MAX_IMAGE_BYTES=24*1024*1024; MAX_RESPONSE_BYTES=128*1024
PROMPT=("Detect EVERY visible deer and analyze each one separately for human profile review. Return one animals entry per deer, ordered left-to-right, with a tight normalized [0,1] bounding box around the complete visible animal. "
"animal_count must equal the number of animals entries. detection_complete is false whenever a deer may be missed, two deer cannot be separated, or a boundary is uncertain. Describe that problem in detection_notes. "
"For each animal describe only evidence visible at this angle. "
"Count clearly visible antler tines/points separately for the image-left and image-right antler; never infer hidden points and explain occlusion. "
"Describe main-beam sweep/profile, tine layout, apparent mass, spread impression, asymmetry, breakage, velvet/hard-antler condition, and identity marks. "
"Frontal ears-relaxed views are useful for spread; broadside views for beam/tine profile and body aging. Without a scale and adequate complementary angles, antler_score_eligible must be false and range unknown. "
"Age from BODY morphology only (neck/shoulder junction, chest depth, back/belly line, leg-length impression), never antler size. Use only 1.5, 2.5, 3.5, mature 4.5+, or unknown. "
"Axis cycles are asynchronous, so do not infer age/season from antler stage. Never copy one deer's identity evidence to another. Return only schema JSON.")
ENUMS={"species":["whitetail","axis","other_deer","non_deer","unknown"],"sex":["male","female","unknown"],"view_angle":["frontal","quartering_left","quartering_right","broadside_left","broadside_right","rear","unknown"],"visibility":["full","partial","obscured","not_visible"],"age_class":["1_5","2_5","3_5","mature_4_5_plus","unknown"],"antler_condition":["hard_antler","velvet","shed_or_absent","unknown"]}
ANIMAL_PROPERTIES={
 "instance_index":{"type":"integer","minimum":1,"maximum":20},
 "bbox":{"type":"object","properties":{"x":{"type":"number","minimum":0,"maximum":1},"y":{"type":"number","minimum":0,"maximum":1},"width":{"type":"number","exclusiveMinimum":0,"maximum":1},"height":{"type":"number","exclusiveMinimum":0,"maximum":1}},"required":["x","y","width","height"],"additionalProperties":False},
 "species":{"type":"string","enum":ENUMS["species"]},"sex":{"type":"string","enum":ENUMS["sex"]},"identity_eligible":{"type":"boolean"},
 "view_angle":{"type":"string","enum":ENUMS["view_angle"]},"head_visibility":{"type":"string","enum":ENUMS["visibility"]},"body_visibility":{"type":"string","enum":ENUMS["visibility"]},
 "visible_tines_left":{"type":"integer","minimum":0,"maximum":30},"visible_tines_right":{"type":"integer","minimum":0,"maximum":30},"tine_count_limitations":{"type":"string","maxLength":300},
 "antler_structure":{"type":"string","maxLength":500},"beam_observation":{"type":"string","maxLength":300},"mass_observation":{"type":"string","maxLength":300},"spread_observation":{"type":"string","maxLength":300},"asymmetry_or_damage":{"type":"string","maxLength":300},"antler_condition":{"type":"string","enum":ENUMS["antler_condition"]},
 "age_eligible":{"type":"boolean"},"age_class":{"type":"string","enum":ENUMS["age_class"]},"age_cues":{"type":"array","items":{"type":"string","maxLength":160},"maxItems":8},
 "antler_score_eligible":{"type":"boolean"},"antler_score_range":{"type":"string","maxLength":80},"score_limitations":{"type":"string","maxLength":300},
 "distinguishing_features":{"type":"array","items":{"type":"string","maxLength":120},"maxItems":12},"summary":{"type":"string","maxLength":800}}
ANIMAL_SCHEMA={"type":"object","properties":ANIMAL_PROPERTIES,"required":list(ANIMAL_PROPERTIES),"additionalProperties":False}
SCHEMA={"type":"object","properties":{"animal_count":{"type":"integer","minimum":0,"maximum":20},"detection_complete":{"type":"boolean"},"detection_notes":{"type":"string","maxLength":500},"animals":{"type":"array","items":ANIMAL_SCHEMA,"maxItems":20}},"required":["animal_count","detection_complete","detection_notes","animals"],"additionalProperties":False}
ANIMAL_FIELDS=set(ANIMAL_PROPERTIES)
LEGACY_FIELDS=ANIMAL_FIELDS-{"instance_index","bbox"}
def _normalize_animal(value:Mapping[str,Any])->dict[str,Any]:
 if not isinstance(value,Mapping) or set(value)!=ANIMAL_FIELDS: raise ModelUnavailable("HD animal fields are invalid")
 for f,k in (("species","species"),("sex","sex"),("view_angle","view_angle"),("head_visibility","visibility"),("body_visibility","visibility"),("age_class","age_class"),("antler_condition","antler_condition")):
  if value[f] not in ENUMS[k]: raise ModelUnavailable("HD classification is invalid")
 for f,hi in (("visible_tines_left",30),("visible_tines_right",30)):
  if isinstance(value[f],bool) or not isinstance(value[f],int) or not 0<=value[f]<=hi: raise ModelUnavailable("HD count is invalid")
 for f in ("identity_eligible","age_eligible","antler_score_eligible"):
  if not isinstance(value[f],bool): raise ModelUnavailable("HD eligibility is invalid")
 for f,limit in (("tine_count_limitations",300),("antler_structure",500),("beam_observation",300),("mass_observation",300),("spread_observation",300),("asymmetry_or_damage",300),("antler_score_range",80),("score_limitations",300),("summary",800)):
  if not isinstance(value[f],str) or not value[f].strip() or len(value[f])>limit: raise ModelUnavailable("HD narrative is invalid")
 for f,maxn,limit in (("age_cues",8,160),("distinguishing_features",12,120)):
  if not isinstance(value[f],list) or len(value[f])>maxn or any(not isinstance(x,str) or not x.strip() or len(x)>limit for x in value[f]): raise ModelUnavailable("HD evidence list is invalid")
 if not value["age_eligible"] and value["age_class"]!="unknown": raise ModelUnavailable("ineligible age must remain unknown")
 if not value["antler_score_eligible"] and value["antler_score_range"]!="unknown": raise ModelUnavailable("ineligible antler score must remain unknown")
 box=value["bbox"]
 if not isinstance(value["instance_index"],int) or isinstance(value["instance_index"],bool) or not 1<=value["instance_index"]<=20: raise ModelUnavailable("HD animal index is invalid")
 if not isinstance(box,Mapping) or set(box)!={"x","y","width","height"}: raise ModelUnavailable("HD bounding box is invalid")
 if any(isinstance(box[k],bool) or not isinstance(box[k],(int,float)) for k in box): raise ModelUnavailable("HD bounding box is invalid")
 x,y,w,h=(float(box[k]) for k in ("x","y","width","height"))
 if x<0 or y<0 or w<=0 or h<=0 or x+w>1 or y+h>1: raise ModelUnavailable("HD bounding box is invalid")
 return {k:([x.strip() for x in v] if isinstance(v,list) else v.strip() if isinstance(v,str) else {q:float(z) for q,z in v.items()} if k=="bbox" else v) for k,v in value.items()}
def normalize_result(value:Mapping[str,Any])->dict[str,Any]:
 if not isinstance(value,Mapping): raise ModelUnavailable("HD model result fields are invalid")
 # Read old persisted/test results safely; all new model output uses animal instances.
 if set(value)==LEGACY_FIELDS|{"animal_count"}:
  animal=_normalize_animal({"instance_index":1,"bbox":{"x":0.0,"y":0.0,"width":1.0,"height":1.0},**{k:value[k] for k in LEGACY_FIELDS}})
  legacy={k:v for k,v in animal.items() if k not in {"instance_index","bbox"}}
  return {**legacy,"animal_count":value["animal_count"]}
 if set(value)!=set(SCHEMA["required"]): raise ModelUnavailable("HD model result fields are invalid")
 count=value["animal_count"]
 if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=20 or not isinstance(value["detection_complete"],bool): raise ModelUnavailable("HD detection summary is invalid")
 if not isinstance(value["detection_notes"],str) or not value["detection_notes"].strip() or len(value["detection_notes"])>500 or not isinstance(value["animals"],list) or len(value["animals"])!=count: raise ModelUnavailable("HD detection summary is invalid")
 animals=[_normalize_animal(item) for item in value["animals"]]
 if [item["instance_index"] for item in animals]!=list(range(1,count+1)): raise ModelUnavailable("HD animal indexes are invalid")
 return {"animal_count":count,"detection_complete":value["detection_complete"],"detection_notes":value["detection_notes"].strip(),"animals":animals}
class OpenAIHDAnalyzer:
 def __init__(self,api_key:str,model:str,deadline:Optional[float]=None,clock:Callable[[],float]=time.monotonic):
  if not isinstance(api_key,str) or not api_key.strip(): raise ValueError("OpenAI API key is required")
  if model!=DEFAULT_MODEL: raise ValueError("HD analyzer must use the pinned OpenAI snapshot")
  self.api_key=api_key.strip();self.model=model;self.deadline=deadline;self.clock=clock
 def analyze(self,image:bytes)->dict[str,Any]:
  if not image or len(image)>MAX_IMAGE_BYTES: raise ModelUnavailable("HD image is invalid")
  body=json.dumps({"model":self.model,"messages":[{"role":"user","content":[{"type":"text","text":PROMPT},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+base64.b64encode(image).decode(),"detail":"high"}}]}],"response_format":{"type":"json_schema","json_schema":{"name":"deerid_hd_analysis","strict":True,"schema":SCHEMA}},"temperature":0,"max_tokens":5000},separators=(",",":")).encode()
  request=urllib.request.Request(OPENAI_ENDPOINT,data=body,headers={"Content-Type":"application/json","Authorization":"Bearer "+self.api_key},method="POST")
  try:
   with urllib.request.urlopen(request,timeout=180) as response: payload=response.read(MAX_RESPONSE_BYTES+1)
  except (OSError,urllib.error.URLError) as exc: raise ModelUnavailable("OpenAI HD model unavailable") from exc
  if len(payload)>MAX_RESPONSE_BYTES: raise ModelUnavailable("HD model response is too large")
  try: return normalize_result(json.loads(json.loads(payload)["choices"][0]["message"]["content"]))
  except (KeyError,TypeError,ValueError,json.JSONDecodeError) as exc: raise ModelUnavailable("HD model response is malformed") from exc
def run_worker(environ:Mapping[str,str],*,media_asset_id:Optional[str]=None,catalog_factory:Callable[...,Any]=SupabaseCatalog,analyzer_factory:Callable[...,Any]=OpenAIHDAnalyzer,deadline:Optional[float]=None,clock:Callable[[],float]=time.monotonic)->dict[str,Any]:
 if not environ.get("SUPABASE_URL") or not environ.get("SUPABASE_SECRET_KEY"): raise ValueError("Supabase configuration is required")
 if not environ.get("OPENAI_API_KEY"): raise ValueError("OpenAI configuration is required")
 catalog=catalog_factory(environ["SUPABASE_URL"],environ["SUPABASE_SECRET_KEY"],environ.get("SUPABASE_BUCKET","tactacam-photos"));catalog.set_deadline(deadline or clock()+900,clock=clock);claim=catalog.claim_hd_review(MODEL_NAME,MODEL_VERSION,media_asset_id)
 if not isinstance(claim,Mapping) or not claim.get("ok"): raise RuntimeError("HD review claim failed")
 if claim.get("empty"): return {"ok":True,"empty":True,"completed":0,"failed":0}
 token=claim.get("claim_token")
 try:
  image=catalog.read_private_image(claim["object_path"],max_bytes=MAX_IMAGE_BYTES);analyzer=analyzer_factory(environ["OPENAI_API_KEY"],DEFAULT_MODEL,deadline,clock);result=analyzer.analyze(image);completed=catalog.complete_hd_review(token,MODEL_NAME,MODEL_VERSION,result)
  if not isinstance(completed,Mapping) or not completed.get("ok"): raise RuntimeError("HD result recording failed")
 except ModelUnavailable:
  catalog.fail_hd_review(token,"model_unavailable");return {"ok":False,"empty":False,"completed":0,"failed":1}
 return {"ok":True,"empty":False,"completed":1,"failed":0}
def handle_trigger(environ:Mapping[str,str],authorization:Optional[str],media_asset_id:str,**kwargs:Any)->tuple[int,dict[str,Any]]:
 secret=environ.get("HD_ANALYSIS_TRIGGER_SECRET","")
 expected="Bearer "+secret
 if len(secret)<16 or not isinstance(authorization,str) or not hmac.compare_digest(authorization,expected): return 401,{"ok":False,"error":"unauthorized"}
 if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",media_asset_id or "") is None: return 400,{"ok":False,"error":"invalid asset"}
 result=run_worker(environ,media_asset_id=media_asset_id,**kwargs)
 return (200 if result.get("ok") else 503),result
def main()->int: print(json.dumps(run_worker(os.environ),sort_keys=True));return 0
if __name__=="__main__": raise SystemExit(main())
