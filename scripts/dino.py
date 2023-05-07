import os
import gc
import cv2
import copy
import torch
from collections import OrderedDict

from modules import scripts, shared
from modules.devices import device, torch_gc, cpu


dino_model_cache = OrderedDict()
dino_model_dir = os.path.join(scripts.basedir(), "models/grounding-dino")
dino_model_list = ["GroundingDINO_SwinT_OGC (694MB)", "GroundingDINO_SwinB (938MB)"]
dino_model_info = {
    "GroundingDINO_SwinT_OGC (694MB)": {
        "checkpoint": "groundingdino_swint_ogc.pth",
        "config": os.path.join(dino_model_dir, "GroundingDINO_SwinT_OGC.py"),
        "url": "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/groundingdino_swint_ogc.pth",
    },
    "GroundingDINO_SwinB (938MB)": {
        "checkpoint": "groundingdino_swinb_cogcoor.pth",
        "config": os.path.join(dino_model_dir, "GroundingDINO_SwinB.cfg.py"),
        "url": "https://huggingface.co/ShilongLiu/GroundingDINO/resolve/main/groundingdino_swinb_cogcoor.pth"
    },
}

dino_install_issue_text = "submit an issue to https://github.com/IDEA-Research/Grounded-Segment-Anything/issues."


def install_goundingdino():
    import launch
    if launch.is_installed("groundingdino"):
        return True
    try:
        launch.run_pip(
            f"install git+https://github.com/IDEA-Research/GroundingDINO",
            f"sd-webui-segment-anything requirement: groundingdino")
        print("GroundingDINO install success.")
        return True
    except Exception:
        import traceback
        print(traceback.print_exc())
        print(f"GroundingDINO install failed. Please {dino_install_issue_text}")
        return False


def show_boxes(image_np, boxes, color=(255, 0, 0, 255), thickness=2, show_index=False):
    if boxes is None:
        return image_np

    image = copy.deepcopy(image_np)
    for idx, box in enumerate(boxes):
        x, y, w, h = box
        cv2.rectangle(image, (x, y), (w, h), color, thickness)
        if show_index:
            font = cv2.FONT_HERSHEY_SIMPLEX
            text = str(idx)
            textsize = cv2.getTextSize(text, font, 1, 2)[0]
            cv2.putText(image, text, (x, y+textsize[1]), font, 1, color, thickness)

    return image


def clear_dino_cache():
    dino_model_cache.clear()
    gc.collect()
    torch_gc()


def load_dino_model(dino_checkpoint):
    print(f"Initializing GroundingDINO {dino_checkpoint}")
    if dino_checkpoint in dino_model_cache:
        dino = dino_model_cache[dino_checkpoint]
        if shared.cmd_opts.lowvram:
            dino.to(device=device)
    else:
        clear_dino_cache()
        from groundingdino.models import build_model
        from groundingdino.util.slconfig import SLConfig
        from groundingdino.util.utils import clean_state_dict
        args = SLConfig.fromfile(dino_model_info[dino_checkpoint]["config"])
        dino = build_model(args)
        checkpoint = torch.hub.load_state_dict_from_url(
            dino_model_info[dino_checkpoint]["url"], dino_model_dir)
        dino.load_state_dict(clean_state_dict(
            checkpoint['model']), strict=False)
        # dino.to(device="cpu")
        dino.to(device=device)
        dino_model_cache[dino_checkpoint] = dino
    dino.eval()
    return dino


def load_dino_image(image_pil):
    import groundingdino.datasets.transforms as T
    transform = T.Compose(
        [
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    image, _ = transform(image_pil, None)  # 3, h, w
    return image


def get_grounding_output(model, image, caption, box_threshold):
    caption = caption.lower()
    caption = caption.strip()
    if not caption.endswith("."):
        caption = caption + "."
    image = image.to(device)
    with torch.no_grad():
        outputs = model(image[None], captions=[caption])
    if shared.cmd_opts.lowvram:
        model.to(cpu)
    logits = outputs["pred_logits"].sigmoid()[0]  # (nq, 256)
    boxes = outputs["pred_boxes"][0]  # (nq, 4)

    # filter output
    logits_filt = logits.clone()
    boxes_filt = boxes.clone()
    filt_mask = logits_filt.max(dim=1)[0] > box_threshold
    logits_filt = logits_filt[filt_mask]  # num_filt, 256
    boxes_filt = boxes_filt[filt_mask]  # num_filt, 4

    return boxes_filt.cpu()


def dino_predict_internal(input_image, dino_model_name, text_prompt, box_threshold):
    install_success = install_goundingdino()
    if not install_success:
        return None, False
    print("Running GroundingDINO Inference")
    dino_image = load_dino_image(input_image.convert("RGB"))
    dino_model = load_dino_model(dino_model_name)

    boxes_filt = get_grounding_output(
        dino_model, dino_image, text_prompt, box_threshold
    )

    H, W = input_image.size[1], input_image.size[0]
    for i in range(boxes_filt.size(0)):
        boxes_filt[i] = boxes_filt[i] * torch.Tensor([W, H, W, H])
        boxes_filt[i][:2] -= boxes_filt[i][2:] / 2
        boxes_filt[i][2:] += boxes_filt[i][:2]
    gc.collect()
    torch_gc()
    return boxes_filt, True
