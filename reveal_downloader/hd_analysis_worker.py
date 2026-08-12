"""Local returned-HD analysis worker for human profile review."""
from __future__ import annotations
import base64,json,os,time,urllib.error,urllib.request
from typing import Any,Callable,Mapping,Optional
from .catalog import SupabaseCatalog
from .gate1b_worker import DEFAULT_ENDPOINT,DEFAULT_MODEL,MODEL_DIGEST,ModelUnavailable
MODEL_NAME="Ollama-Gemma4-Vision-HD"
MODEL_VERSION="gemma4-e4b-c6eb396dbd59@hd-prompt-2026-08-12.2"
MAX_IMAGE_BYTES=24*1024*1024; MAX_RESPONSE_BYTES=128*1024
PROMPT=("Analyze EACH visible deer separately for human profile review. Describe only evidence visible at this angle. "
"Count clearly visible antler tines/points separately for the image-left and image-right antler; never infer hidden points and explain occlusion. "
"Describe main-beam sweep/profile, tine layout, apparent mass, spread impression, asymmetry, breakage, velvet/hard-antler condition, and identity marks. "
"Frontal ears-relaxed views are useful for spread; broadside views for beam/tine profile and body aging. Without a scale and adequate complementary angles, antler_score_eligible must be false and range unknown. "
"Age from BODY morphology only (neck/shoulder junction, chest depth, back/belly line, leg-length impression), never antler size. Use only 1.5, 2.5, 3.5, mature 4.5+, or unknown. "
"Axis cycles are asynchronous, so do not infer age/season from antler stage. If multiple deer appear, summarize the most identity-worthy animal and state that animal-level review is required. Return only schema JSON.")
ENUMS={"species":["whitetail","axis","other_deer","non_deer","unknown"],"sex":["male","female","unknown"],"view_angle":["frontal","quartering_left","quartering_right","broadside_left","broadside_right","rear","unknown"],"visibility":["full","partial","obscured","not_visible"],"age_class":["1_5","2_5","3_5","mature_4_5_plus","unknown"],"antler_condition":["hard_antler","velvet","shed_or_absent","unknown"]}
SCHEMA={"type":"object","properties":{
 "species":{"type":"string","enum":ENUMS["species"]},"sex":{"type":"string","enum":ENUMS["sex"]},"animal_count":{"type":"integer","minimum":0,"maximum":20},"identity_eligible":{"type":"boolean"},
 "view_angle":{"type":"string","enum":ENUMS["view_angle"]},"head_visibility":{"type":"string","enum":ENUMS["visibility"]},"body_visibility":{"type":"string","enum":ENUMS["visibility"]},
 "visible_tines_left":{"type":"integer","minimum":0,"maximum":30},"visible_tines_right":{"type":"integer","minimum":0,"maximum":30},"tine_count_limitations":{"type":"string","maxLength":300},
 "antler_structure":{"type":"string","maxLength":500},"beam_observation":{"type":"string","maxLength":300},"mass_observation":{"type":"string","maxLength":300},"spread_observation":{"type":"string","maxLength":300},"asymmetry_or_damage":{"type":"string","maxLength":300},"antler_condition":{"type":"string","enum":ENUMS["antler_condition"]},
 "age_eligible":{"type":"boolean"},"age_class":{"type":"string","enum":ENUMS["age_class"]},"age_cues":{"type":"array","items":{"type":"string","maxLength":160},"maxItems":8},
 "antler_score_eligible":{"type":"boolean"},"antler_score_range":{"type":"string","maxLength":80},"score_limitations":{"type":"string","maxLength":300},
 "distinguishing_features":{"type":"array","items":{"type":"string","maxLength":120},"maxItems":12},"summary":{"type":"string","maxLength":800}},
 "required":[],"additionalProperties":False}
SCHEMA["required"]=list(SCHEMA["properties"])
def normalize_result(value:Mapping[str,Any])->dict[str,Any]:
 if not isinstance(value,Mapping) or set(value)!=set(SCHEMA["required"]): raise ModelUnavailable("HD model result fields are invalid")
 for f,k in (("species","species"),("sex","sex"),("view_angle","view_angle"),("head_visibility","visibility"),("body_visibility","visibility"),("age_class","age_class"),("antler_condition","antler_condition")):
  if value[f] not in ENUMS[k]: raise ModelUnavailable("HD classification is invalid")
 for f,hi in (("animal_count",20),("visible_tines_left",30),("visible_tines_right",30)):
  if isinstance(value[f],bool) or not isinstance(value[f],int) or not 0<=value[f]<=hi: raise ModelUnavailable("HD count is invalid")
 for f in ("identity_eligible","age_eligible","antler_score_eligible"):
  if not isinstance(value[f],bool): raise ModelUnavailable("HD eligibility is invalid")
 for f,limit in (("tine_count_limitations",300),("antler_structure",500),("beam_observation",300),("mass_observation",300),("spread_observation",300),("asymmetry_or_damage",300),("antler_score_range",80),("score_limitations",300),("summary",800)):
  if not isinstance(value[f],str) or not value[f].strip() or len(value[f])>limit: raise ModelUnavailable("HD narrative is invalid")
 for f,maxn,limit in (("age_cues",8,160),("distinguishing_features",12,120)):
  if not isinstance(value[f],list) or len(value[f])>maxn or any(not isinstance(x,str) or not x.strip() or len(x)>limit for x in value[f]): raise ModelUnavailable("HD evidence list is invalid")
 if not value["age_eligible"] and value["age_class"]!="unknown": raise ModelUnavailable("ineligible age must remain unknown")
 if not value["antler_score_eligible"] and value["antler_score_range"]!="unknown": raise ModelUnavailable("ineligible antler score must remain unknown")
 return {k:([x.strip() for x in v] if isinstance(v,list) else v.strip() if isinstance(v,str) else v) for k,v in value.items()}
class OllamaHDAnalyzer:
 def __init__(self,endpoint:str,model:str,deadline:Optional[float]=None,clock:Callable[[],float]=time.monotonic):
  if endpoint.rstrip("/") not in {"http://127.0.0.1:11434","http://localhost:11434"} or model!=DEFAULT_MODEL: raise ValueError("HD analyzer must use the pinned local model")
  self.endpoint=endpoint.rstrip("/");self.model=model;self.deadline=deadline;self.clock=clock
  with urllib.request.urlopen(f"{self.endpoint}/api/tags",timeout=5) as response: tags=json.load(response)
  if not any(x.get("name")==DEFAULT_MODEL and x.get("digest")==MODEL_DIGEST for x in tags.get("models",[])): raise ModelUnavailable("local model digest is not pinned")
 def analyze(self,image:bytes)->dict[str,Any]:
  if not image or len(image)>MAX_IMAGE_BYTES: raise ModelUnavailable("HD image is invalid")
  body=json.dumps({"model":self.model,"prompt":PROMPT,"images":[base64.b64encode(image).decode()],"stream":False,"format":SCHEMA,"options":{"temperature":0}},separators=(",",":")).encode()
  request=urllib.request.Request(f"{self.endpoint}/api/generate",data=body,headers={"Content-Type":"application/json"},method="POST")
  try:
   with urllib.request.urlopen(request,timeout=180) as response: payload=response.read(MAX_RESPONSE_BYTES+1)
  except (OSError,urllib.error.URLError) as exc: raise ModelUnavailable("local HD model unavailable") from exc
  if len(payload)>MAX_RESPONSE_BYTES: raise ModelUnavailable("HD model response is too large")
  try: return normalize_result(json.loads(json.loads(payload)["response"]))
  except (KeyError,TypeError,ValueError,json.JSONDecodeError) as exc: raise ModelUnavailable("HD model response is malformed") from exc
def run_worker(environ:Mapping[str,str],*,catalog_factory:Callable[...,Any]=SupabaseCatalog,analyzer_factory:Callable[...,Any]=OllamaHDAnalyzer,deadline:Optional[float]=None,clock:Callable[[],float]=time.monotonic)->dict[str,Any]:
 if not environ.get("SUPABASE_URL") or not environ.get("SUPABASE_SECRET_KEY"): raise ValueError("Supabase configuration is required")
 catalog=catalog_factory(environ["SUPABASE_URL"],environ["SUPABASE_SECRET_KEY"],environ.get("SUPABASE_BUCKET","tactacam-photos"));catalog.set_deadline(deadline or clock()+900,clock=clock);claim=catalog.claim_hd_review(MODEL_NAME,MODEL_VERSION)
 if not isinstance(claim,Mapping) or not claim.get("ok"): raise RuntimeError("HD review claim failed")
 if claim.get("empty"): return {"ok":True,"empty":True,"completed":0,"failed":0}
 token=claim.get("claim_token")
 try:
  image=catalog.read_private_image(claim["object_path"],max_bytes=MAX_IMAGE_BYTES);analyzer=analyzer_factory(environ.get("HD_OLLAMA_URL",DEFAULT_ENDPOINT),DEFAULT_MODEL,deadline,clock);result=analyzer.analyze(image);completed=catalog.complete_hd_review(token,MODEL_NAME,MODEL_VERSION,result)
  if not isinstance(completed,Mapping) or not completed.get("ok"): raise RuntimeError("HD result recording failed")
 except ModelUnavailable:
  catalog.fail_hd_review(token,"model_unavailable");return {"ok":False,"empty":False,"completed":0,"failed":1}
 return {"ok":True,"empty":False,"completed":1,"failed":0}
def main()->int: print(json.dumps(run_worker(os.environ),sort_keys=True));return 0
if __name__=="__main__": raise SystemExit(main())
