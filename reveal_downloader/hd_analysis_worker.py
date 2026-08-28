"""Local returned-HD analysis worker for human profile review."""
from __future__ import annotations
import base64,contextlib,hashlib,hmac,importlib.util,io,json,math,os,re,tempfile,time,urllib.error,urllib.request
from io import BytesIO
from PIL import Image,ImageOps
from typing import Any,Callable,Mapping,Optional
from .catalog import SupabaseCatalog
from .gate1b_worker import OPENAI_ENDPOINT,ModelUnavailable
HD_DESCRIPTION_MODEL="gpt-5.4-mini-2026-03-17"
DEFAULT_MODEL=HD_DESCRIPTION_MODEL
MEGADETECTOR_MODEL="MDV6-yolov9-c"
MODEL_NAME="MegaDetector-V6-plus-OpenAI-GPT-5.4-mini-HD"
MODEL_VERSION=f"megadetector-v6-{MEGADETECTOR_MODEL.lower()}@gpt-5.4-mini-2026-03-17@hd-crops-2026-08-12.3"
FALLBACK_MODEL_NAME="OpenAI-GPT-5.4-mini-localization-plus-HD-crops"
FALLBACK_MODEL_VERSION="gpt-5.4-mini-2026-03-17@localization-and-hd-crops-2026-08-13.1"
MAX_IMAGE_BYTES=24*1024*1024; MAX_RESPONSE_BYTES=128*1024
LOCALIZATION_PROMPT=("Detect EVERY visible deer. Return one animals entry per deer, ordered left-to-right. Each normalized [0,1] bounding box must contain the complete visible deer, including every visible antler tip, ear, nose, leg, hoof, tail, and body edge; prefer extra background over clipping anatomy. "
"animal_count must equal the number of animals entries. detection_complete is false whenever a deer may be missed, two deer cannot be separated, or a boundary is uncertain. Describe that problem in detection_notes. "
"Return only schema JSON.")
CROP_PROMPT=("Analyze only the primary deer centered in this physically cropped image. Ignore any partial or background deer at the crop edges. Every description, count, age cue, and distinguishing feature must belong to this one centered deer; abstain when attribution is uncertain. The crop includes safety margin so inspect continuous antlers and body extending around the center. "
"Describe only evidence visible at this angle. "
"Count clearly visible antler tines/points separately for the image-left and image-right antler; never infer hidden points and explain occlusion. "
"Describe main-beam sweep/profile, tine layout, apparent mass, spread impression, asymmetry, breakage, velvet/hard-antler condition, and identity marks. "
"Frontal ears-relaxed views are useful for spread; broadside views for beam/tine profile and body aging. Without a scale and adequate complementary angles, antler_score_eligible must be false and range unknown. "
"Age from BODY morphology only (neck/shoulder junction, chest depth, back/belly line, leg-length impression), never antler size. Use only 1.5, 2.5, 3.5, mature 4.5+, or unknown. "
"Axis cycles are asynchronous, so do not infer age/season from antler stage. Return only schema JSON.")
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
LOCALIZATION_ANIMAL_PROPERTIES={"instance_index":ANIMAL_PROPERTIES["instance_index"],"bbox":ANIMAL_PROPERTIES["bbox"]}
LOCALIZATION_SCHEMA={"type":"object","properties":{"animal_count":{"type":"integer","minimum":0,"maximum":20},"detection_complete":{"type":"boolean"},"detection_notes":{"type":"string","maxLength":500},"animals":{"type":"array","items":{"type":"object","properties":LOCALIZATION_ANIMAL_PROPERTIES,"required":["instance_index","bbox"],"additionalProperties":False},"maxItems":20}},"required":["animal_count","detection_complete","detection_notes","animals"],"additionalProperties":False}
CROP_PROPERTIES={k:v for k,v in ANIMAL_PROPERTIES.items() if k not in {"instance_index","bbox"}}
CROP_SCHEMA={"type":"object","properties":CROP_PROPERTIES,"required":list(CROP_PROPERTIES),"additionalProperties":False}
SCHEMA={"type":"object","properties":{"animal_count":{"type":"integer","minimum":0,"maximum":20},"detection_complete":{"type":"boolean"},"detection_notes":{"type":"string","maxLength":500},"animals":{"type":"array","items":ANIMAL_SCHEMA,"maxItems":20}},"required":["animal_count","detection_complete","detection_notes","animals"],"additionalProperties":False}
ANIMAL_FIELDS=set(ANIMAL_PROPERTIES)
LEGACY_FIELDS=ANIMAL_FIELDS-{"instance_index","bbox"}
class LocalizerDependencyUnavailable(ModelUnavailable): pass
def _validated_bbox(box:Any)->dict[str,float]:
 if not isinstance(box,Mapping) or set(box)!={"x","y","width","height"}: raise ModelUnavailable("HD bounding box is invalid")
 if any(isinstance(box[k],bool) or not isinstance(box[k],(int,float)) for k in box): raise ModelUnavailable("HD bounding box is invalid")
 x,y,w,h=(float(box[k]) for k in ("x","y","width","height"))
 if not all(math.isfinite(value) for value in (x,y,w,h)) or x<0 or y<0 or w<=0 or h<=0 or x+w>1 or y+h>1: raise ModelUnavailable("HD bounding box is invalid")
 return {"x":x,"y":y,"width":w,"height":h}
def _bbox_iou(a:Mapping[str,float],b:Mapping[str,float])->float:
 left=max(a["x"],b["x"]);top=max(a["y"],b["y"]);right=min(a["x"]+a["width"],b["x"]+b["width"]);bottom=min(a["y"]+a["height"],b["y"]+b["height"])
 intersection=max(0.0,right-left)*max(0.0,bottom-top)
 return intersection/(a["width"]*a["height"]+b["width"]*b["height"]-intersection) if intersection else 0.0
def padded_bbox(box:Mapping[str,Any])->dict[str,float]:
 box=_validated_bbox(box);x,y,w,h=(box[k] for k in ("x","y","width","height"))
 # MegaDetector boxes are deliberately expanded, with extra head/antler room.
 left=max(0.0,x-w*.25);top=max(0.0,y-h*.35);right=min(1.0,x+w*1.25);bottom=min(1.0,y+h*1.15)
 return {"x":left,"y":top,"width":right-left,"height":bottom-top}
def crop_animal_image(image:bytes,box:Mapping[str,Any])->bytes:
 try:
  with Image.open(BytesIO(image)) as source:
   source=ImageOps.exif_transpose(source).convert("RGB");width,height=source.size
   x1=max(0,min(width-1,round(float(box["x"])*width)));y1=max(0,min(height-1,round(float(box["y"])*height)))
   x2=max(x1+1,min(width,round((float(box["x"])+float(box["width"]))*width)));y2=max(y1+1,min(height,round((float(box["y"])+float(box["height"]))*height)))
   cropped=source.crop((x1,y1,x2,y2));out=BytesIO();cropped.save(out,format="JPEG",quality=92,optimize=True);return out.getvalue()
 except (OSError,KeyError,TypeError,ValueError) as exc: raise ModelUnavailable("HD image crop is invalid") from exc
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
 box=_validated_bbox(value["bbox"])
 if not isinstance(value["instance_index"],int) or isinstance(value["instance_index"],bool) or not 1<=value["instance_index"]<=20: raise ModelUnavailable("HD animal index is invalid")
 return {k:([x.strip() for x in v] if isinstance(v,list) else v.strip() if isinstance(v,str) else box if k=="bbox" else v) for k,v in value.items()}
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
class MegaDetectorLocalizer:
 """Authoritative MegaDetector V6 animal localization adapter."""
 def __init__(self,detector:Optional[Callable[[str],Any]]=None,confidence_threshold:float=.15):
  self.detector=detector;self.confidence_threshold=confidence_threshold
 def _default_detector(self,path:str)->Any:
  try:
   from PytorchWildlife.models import detection as pw_detection
  except ModuleNotFoundError as exc:
   if exc.name and (exc.name=="PytorchWildlife" or exc.name.startswith("PytorchWildlife.")): raise LocalizerDependencyUnavailable("PytorchWildlife is required for HD localization") from exc
   raise ModelUnavailable("MegaDetector dependency is broken") from exc
  except ImportError as exc: raise ModelUnavailable("MegaDetector dependency is broken") from exc
  # Ultralytics writes progress/model banners to stdout; keep worker stdout JSON-only.
  with contextlib.redirect_stdout(io.StringIO()):
   if not hasattr(self,"_model"):
    self._model=pw_detection.MegaDetectorV6(device="cpu",version=MEGADETECTOR_MODEL)
   return self._model.single_image_detection(path,det_conf_thres=self.confidence_threshold)
 @staticmethod
 def _rows(raw:Any)->list[dict[str,Any]]:
  if not isinstance(raw,Mapping): raise ModelUnavailable("MegaDetector response is invalid")
  if isinstance(raw.get("normalized_coords"),list):
   detections=raw.get("detections")
   if detections is None: raise ModelUnavailable("MegaDetector response is invalid")
   classes=list(getattr(detections,"class_id",[]));scores=list(getattr(detections,"confidence",[]))
   if len(classes)!=len(raw["normalized_coords"]) or len(scores)!=len(classes): raise ModelUnavailable("MegaDetector response is invalid")
   return [{"category":"animal" if int(classes[i])==0 else "person" if int(classes[i])==1 else "vehicle","conf":float(scores[i]),"bbox":list(box),"normalized":True} for i,box in enumerate(raw["normalized_coords"])]
  detections=raw.get("detections")
  if not isinstance(detections,list): raise ModelUnavailable("MegaDetector response is invalid")
  return detections
 def locate(self,image:bytes)->dict[str,Any]:
  try:
   with Image.open(BytesIO(image)) as source: width,height=ImageOps.exif_transpose(source).size
   with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
    handle.write(image);handle.flush();raw=(self.detector or self._default_detector)(handle.name)
  except (OSError,ValueError) as exc: raise ModelUnavailable("MegaDetector could not read HD image") from exc
  boxes=[]
  try:
   for item in self._rows(raw):
    if not isinstance(item,Mapping): raise ModelUnavailable("MegaDetector response is invalid")
    category=str(item.get("category",item.get("label",""))).lower();confidence=float(item.get("conf",item.get("confidence",0)) or 0)
    if category not in {"0","1","animal"} or confidence<self.confidence_threshold: continue
    coords=item.get("bbox")
    if not isinstance(coords,(list,tuple)) or len(coords)!=4: raise ModelUnavailable("MegaDetector response is invalid")
    x1,y1,x2,y2=map(float,coords)
    if not item.get("normalized"):
     x1/=width;y1/=height;x2/=width;y2/=height
    box=_validated_bbox({"x":x1,"y":y1,"width":x2-x1,"height":y2-y1})
    if not any(_bbox_iou(box,prior)>=.9 for prior in boxes): boxes.append(box)
  except ModelUnavailable: raise
  except (IndexError,AttributeError,TypeError,ValueError) as exc: raise ModelUnavailable("MegaDetector response is invalid") from exc
  boxes.sort(key=lambda b:b["x"])
  return {"animal_count":len(boxes),"detection_complete":bool(boxes),"detection_notes":f"MegaDetector V6 found {len(boxes)} animal(s) at confidence >= {self.confidence_threshold:.2f}","animals":[{"instance_index":i,"bbox":b} for i,b in enumerate(boxes,1)]}

class OpenAIHDAnalyzer:
 def __init__(self,api_key:str,model:str,deadline:Optional[float]=None,clock:Callable[[],float]=time.monotonic,localizer:Optional[MegaDetectorLocalizer]=None):
  if not isinstance(api_key,str) or not api_key.strip(): raise ValueError("OpenAI API key is required")
  if model!=HD_DESCRIPTION_MODEL: raise ValueError("HD analyzer must use the pinned OpenAI snapshot")
  self.api_key=api_key.strip();self.model=model;self.deadline=deadline;self.clock=clock;self.localizer=localizer or MegaDetectorLocalizer()
 def _request(self,image:bytes,prompt:str,schema:Mapping[str,Any],name:str,max_tokens:int)->dict[str,Any]:
  if not image or len(image)>MAX_IMAGE_BYTES: raise ModelUnavailable("HD image is invalid")
  timeout=180
  if self.deadline is not None:
   remaining=self.deadline-self.clock()-30
   if remaining<10: raise ModelUnavailable("HD analysis deadline exhausted")
   timeout=min(timeout,remaining)
  body=json.dumps({"model":self.model,"messages":[{"role":"user","content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+base64.b64encode(image).decode(),"detail":"high"}}]}],"response_format":{"type":"json_schema","json_schema":{"name":name,"strict":True,"schema":schema}},"max_completion_tokens":max_tokens},separators=(",",":")).encode()
  request=urllib.request.Request(OPENAI_ENDPOINT,data=body,headers={"Content-Type":"application/json","Authorization":"Bearer "+self.api_key},method="POST")
  try:
   with urllib.request.urlopen(request,timeout=timeout) as response: payload=response.read(MAX_RESPONSE_BYTES+1)
  except (OSError,urllib.error.URLError) as exc: raise ModelUnavailable("OpenAI HD model unavailable") from exc
  if len(payload)>MAX_RESPONSE_BYTES: raise ModelUnavailable("HD model response is too large")
  try: return json.loads(json.loads(payload)["choices"][0]["message"]["content"])
  except (KeyError,TypeError,ValueError,json.JSONDecodeError) as exc: raise ModelUnavailable("HD model response is malformed") from exc
 def analyze(self,image:bytes)->dict[str,Any]:
  if hasattr(self,"localizer"):
   try: located=self.localizer.locate(image)
   except LocalizerDependencyUnavailable: located=self._request(image,LOCALIZATION_PROMPT,LOCALIZATION_SCHEMA,"deerid_hd_localization",1800)
  else: located=self._request(image,LOCALIZATION_PROMPT,LOCALIZATION_SCHEMA,"deerid_hd_localization",1800)
  if not isinstance(located,Mapping) or set(located)!=set(LOCALIZATION_SCHEMA["required"]): raise ModelUnavailable("HD localization response is invalid")
  count=located["animal_count"];animals=located["animals"];notes=located["detection_notes"]
  if isinstance(count,bool) or not isinstance(count,int) or not 0<=count<=20 or not isinstance(animals,list) or len(animals)!=count or not isinstance(located["detection_complete"],bool) or not isinstance(notes,str) or not notes.strip() or len(notes)>500: raise ModelUnavailable("HD localization response is invalid")
  boxes=[]
  for index,item in enumerate(animals,1):
   if not isinstance(item,Mapping) or set(item)!={"instance_index","bbox"} or item["instance_index"]!=index: raise ModelUnavailable("HD localization item is invalid")
   box=_validated_bbox(item["bbox"])
   if any(_bbox_iou(box,prior)>=.9 for prior in boxes): raise ModelUnavailable("HD localization contains duplicate animals")
   boxes.append(box)
  results=[]
  for index,raw_box in enumerate(boxes,1):
   box=padded_bbox(raw_box);crop=crop_animal_image(image,box)
   assessment=self._request(crop,CROP_PROMPT,CROP_SCHEMA,"deerid_hd_crop_assessment",2600)
   if not isinstance(assessment,Mapping) or set(assessment)!=set(CROP_PROPERTIES): raise ModelUnavailable("HD crop assessment response is invalid")
   results.append({"instance_index":index,"bbox":box,**assessment})
  return normalize_result({"animal_count":count,"detection_complete":located["detection_complete"],"detection_notes":located["detection_notes"],"animals":results})
def run_worker(environ:Mapping[str,str],*,media_asset_id:Optional[str]=None,catalog_factory:Callable[...,Any]=SupabaseCatalog,analyzer_factory:Callable[...,Any]=OpenAIHDAnalyzer,localizer_available:Callable[[],bool]=lambda:importlib.util.find_spec("PytorchWildlife") is not None,deadline:Optional[float]=None,clock:Callable[[],float]=time.monotonic)->dict[str,Any]:
 if not environ.get("SUPABASE_URL") or not environ.get("SUPABASE_SECRET_KEY"): raise ValueError("Supabase configuration is required")
 if not environ.get("OPENAI_API_KEY"): raise ValueError("OpenAI configuration is required")
 model_name,model_version=(MODEL_NAME,MODEL_VERSION) if localizer_available() else (FALLBACK_MODEL_NAME,FALLBACK_MODEL_VERSION)
 catalog=catalog_factory(environ["SUPABASE_URL"],environ["SUPABASE_SECRET_KEY"],environ.get("SUPABASE_BUCKET","tactacam-photos"));catalog.set_deadline(deadline or clock()+900,clock=clock);claim=catalog.claim_hd_review(model_name,model_version,media_asset_id)
 if not isinstance(claim,Mapping) or not claim.get("ok"): raise RuntimeError("HD review claim failed")
 if claim.get("empty"): return {"ok":True,"empty":True,"completed":0,"failed":0}
 token=claim.get("claim_token")
 try:
  image=catalog.read_private_image(claim["object_path"],max_bytes=MAX_IMAGE_BYTES);analyzer=analyzer_factory(environ["OPENAI_API_KEY"],HD_DESCRIPTION_MODEL,deadline,clock);result=analyzer.analyze(image);completed=catalog.complete_hd_review(token,model_name,model_version,result)
  if not isinstance(completed,Mapping) or not completed.get("ok"): raise RuntimeError("HD result recording failed")
 except ModelUnavailable:
  catalog.fail_hd_review(token,"model_unavailable");return {"ok":False,"empty":False,"completed":0,"failed":1}
 return {"ok":True,"empty":False,"completed":1,"failed":0}
def handle_trigger(environ:Mapping[str,str],authorization:Optional[str],media_asset_id:Optional[str],*,clock:Callable[[],float]=time.monotonic,**kwargs:Any)->tuple[int,dict[str,Any]]:
 secret=environ.get("HD_ANALYSIS_TRIGGER_SECRET","")
 expected="Bearer "+secret
 if len(secret)<16 or not isinstance(authorization,str) or not hmac.compare_digest(authorization,expected): return 401,{"ok":False,"error":"unauthorized"}
 if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",media_asset_id or "") is None: return 400,{"ok":False,"error":"invalid asset"}
 result=run_worker(environ,media_asset_id=media_asset_id,deadline=clock()+270,clock=clock,**kwargs)
 return (200 if result.get("ok") else 503),result
def main()->int: print(json.dumps(run_worker(os.environ),sort_keys=True));return 0
if __name__=="__main__": raise SystemExit(main())
