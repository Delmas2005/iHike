"""Plants and animals in photos, with BioCLIP (via open_clip)."""
from __future__ import annotations

import os
import threading

SPECIES_MODEL = os.environ.get("SPECIES_MODEL", "hf-hub:imageomics/bioclip").strip()

# Common species of European hills and mountains: (common name, scientific name, group).
# BioCLIP compares the photo with each of these descriptions and keeps the closest ones.
SPECIES = [
    # flowers
    ("Edelweiss", "Leontopodium nivale", "flower"), ("Stemless gentian", "Gentiana acaulis", "flower"),
    ("Spring gentian", "Gentiana verna", "flower"), ("Great yellow gentian", "Gentiana lutea", "flower"),
    ("Alpenrose", "Rhododendron ferrugineum", "flower"), ("Arnica", "Arnica montana", "flower"),
    ("Alpine pasqueflower", "Pulsatilla alpina", "flower"), ("Alpine snowbell", "Soldanella alpina", "flower"),
    ("Purple saxifrage", "Saxifraga oppositifolia", "flower"), ("Moss campion", "Silene acaulis", "flower"),
    ("Mountain avens", "Dryas octopetala", "flower"), ("Harebell", "Campanula rotundifolia", "flower"),
    ("Martagon lily", "Lilium martagon", "flower"), ("Globeflower", "Trollius europaeus", "flower"),
    ("Alpine aster", "Aster alpinus", "flower"), ("Alpine sea holly", "Eryngium alpinum", "flower"),
    ("Black vanilla orchid", "Gymnadenia nigra", "flower"), ("Heath spotted orchid", "Dactylorhiza maculata", "flower"),
    ("Lady's slipper orchid", "Cypripedium calceolus", "flower"), ("Glacier crowfoot", "Ranunculus glacialis", "flower"),
    ("Alpine avens", "Geum montanum", "flower"), ("White false hellebore", "Veratrum album", "flower"),
    ("Monkshood", "Aconitum napellus", "flower"), ("Foxglove", "Digitalis purpurea", "flower"),
    ("Spring crocus", "Crocus vernus", "flower"), ("Poet's daffodil", "Narcissus poeticus", "flower"),
    ("Oxeye daisy", "Leucanthemum vulgare", "flower"), ("Yarrow", "Achillea millefolium", "flower"),
    ("Fireweed", "Chamaenerion angustifolium", "flower"), ("Stemless carline thistle", "Carlina acaulis", "flower"),
    ("Cobweb houseleek", "Sempervivum arachnoideum", "flower"), ("Alpine forget-me-not", "Myosotis alpestris", "flower"),
    ("Long-spurred pansy", "Viola calcarata", "flower"), ("Alpine toadflax", "Linaria alpina", "flower"),
    ("Alpine poppy", "Papaver alpinum", "flower"), ("Narcissus-flowered anemone", "Anemone narcissiflora", "flower"),
    ("Marsh marigold", "Caltha palustris", "flower"), ("Grass of Parnassus", "Parnassia palustris", "flower"),
    ("Round-leaved sundew", "Drosera rotundifolia", "flower"), ("Common cottongrass", "Eriophorum angustifolium", "flower"),
    ("Dandelion", "Taraxacum officinale", "flower"), ("Heather", "Calluna vulgaris", "flower"),
    ("Winter heath", "Erica carnea", "flower"), ("Mezereon", "Daphne mezereum", "flower"),
    ("Spiniest thistle", "Cirsium spinosissimum", "flower"), ("Alpine clematis", "Clematis alpina", "flower"),
    ("Bird's-eye primrose", "Primula farinosa", "flower"), ("Mountain houseleek", "Sempervivum montanum", "flower"),
    # shrubs, berries and trees
    ("Bilberry", "Vaccinium myrtillus", "plant"), ("Lingonberry", "Vaccinium vitis-idaea", "plant"),
    ("Wild strawberry", "Fragaria vesca", "plant"), ("Wild raspberry", "Rubus idaeus", "plant"),
    ("Common juniper", "Juniperus communis", "plant"), ("Mountain pine", "Pinus mugo", "tree"),
    ("European larch", "Larix decidua", "tree"), ("Swiss stone pine", "Pinus cembra", "tree"),
    ("Norway spruce", "Picea abies", "tree"), ("Silver fir", "Abies alba", "tree"),
    ("European beech", "Fagus sylvatica", "tree"), ("Silver birch", "Betula pendula", "tree"),
    ("Rowan", "Sorbus aucuparia", "tree"), ("Scots pine", "Pinus sylvestris", "tree"),
    ("Bracken", "Pteridium aquilinum", "plant"),
    # fungi and lichens
    ("Fly agaric", "Amanita muscaria", "fungus"), ("Porcini", "Boletus edulis", "fungus"),
    ("Chanterelle", "Cantharellus cibarius", "fungus"), ("Map lichen", "Rhizocarpon geographicum", "lichen"),
    # mammals
    ("Alpine ibex", "Capra ibex", "mammal"), ("Chamois", "Rupicapra rupicapra", "mammal"),
    ("Alpine marmot", "Marmota marmota", "mammal"), ("Red deer", "Cervus elaphus", "mammal"),
    ("Roe deer", "Capreolus capreolus", "mammal"), ("Red fox", "Vulpes vulpes", "mammal"),
    ("Mountain hare", "Lepus timidus", "mammal"), ("Stoat", "Mustela erminea", "mammal"),
    ("Wild boar", "Sus scrofa", "mammal"), ("Red squirrel", "Sciurus vulgaris", "mammal"),
    ("Brown bear", "Ursus arctos", "mammal"), ("Grey wolf", "Canis lupus", "mammal"),
    ("Eurasian lynx", "Lynx lynx", "mammal"), ("Mouflon", "Ovis gmelini", "mammal"),
    ("Cow", "Bos taurus", "mammal"), ("Sheep", "Ovis aries", "mammal"), ("Goat", "Capra hircus", "mammal"),
    ("Horse", "Equus caballus", "mammal"), ("Dog", "Canis familiaris", "mammal"),
    # birds
    ("Golden eagle", "Aquila chrysaetos", "bird"), ("Bearded vulture", "Gypaetus barbatus", "bird"),
    ("Griffon vulture", "Gyps fulvus", "bird"), ("Alpine chough", "Pyrrhocorax graculus", "bird"),
    ("Common raven", "Corvus corax", "bird"), ("Spotted nutcracker", "Nucifraga caryocatactes", "bird"),
    ("Rock ptarmigan", "Lagopus muta", "bird"), ("Black grouse", "Lyrurus tetrix", "bird"),
    ("Wallcreeper", "Tichodroma muraria", "bird"), ("Alpine accentor", "Prunella collaris", "bird"),
    ("White-winged snowfinch", "Montifringilla nivalis", "bird"), ("Water pipit", "Anthus spinoletta", "bird"),
    ("Northern wheatear", "Oenanthe oenanthe", "bird"), ("Common kestrel", "Falco tinnunculus", "bird"),
    ("Eurasian jay", "Garrulus glandarius", "bird"), ("Black woodpecker", "Dryocopus martius", "bird"),
    ("Common buzzard", "Buteo buteo", "bird"), ("White-throated dipper", "Cinclus cinclus", "bird"),
    # reptiles and amphibians
    ("Alpine salamander", "Salamandra atra", "amphibian"), ("Fire salamander", "Salamandra salamandra", "amphibian"),
    ("Common frog", "Rana temporaria", "amphibian"), ("Common toad", "Bufo bufo", "amphibian"),
    ("Alpine newt", "Ichthyosaura alpestris", "amphibian"), ("Viviparous lizard", "Zootoca vivipara", "reptile"),
    ("Asp viper", "Vipera aspis", "reptile"), ("Common adder", "Vipera berus", "reptile"),
    # insects
    ("Apollo butterfly", "Parnassius apollo", "insect"), ("Swallowtail", "Papilio machaon", "insect"),
    ("Peacock butterfly", "Aglais io", "insect"), ("Small tortoiseshell", "Aglais urticae", "insect"),
    ("Mountain ringlet", "Erebia epiphron", "insect"), ("Common blue", "Polyommatus icarus", "insect"),
    ("Buff-tailed bumblebee", "Bombus terrestris", "insect"), ("Western honey bee", "Apis mellifera", "insect"),
    ("Rosalia longicorn", "Rosalia alpina", "insect"), ("Red wood ant", "Formica rufa", "insect"),
]
# Photos with no living subject: if one of these wins, we say so instead of naming a species.
NOT_A_SPECIES = ["a photo of a mountain landscape", "a photo of rocks and scree", "a photo of a hiking trail",
                 "a photo of a person", "a photo of a building or hut", "a photo of a lake",
                 "a photo of the sky and clouds", "a photo of snow and a glacier", "a photo of a signpost"]

_bioclip = None
_bioclip_lock = threading.Lock()


def _load_bioclip():
    global _bioclip
    if _bioclip is None:
        import open_clip
        import torch

        model, _, preprocess = open_clip.create_model_and_transforms(SPECIES_MODEL)
        tokenizer = open_clip.get_tokenizer(SPECIES_MODEL)
        model.eval()
        prompts = [f"a photo of {sci}, {common.lower()}." for common, sci, _ in SPECIES] + NOT_A_SPECIES
        with torch.no_grad():
            text = model.encode_text(tokenizer(prompts))
            text = text / text.norm(dim=-1, keepdim=True)
        _bioclip = (model, preprocess, text, torch)
    return _bioclip


def identify(image) -> dict:
    """PIL image -> {"verdict": "match" | "unsure" | "none", "species": [{name, scientific, group, score}]}."""
    with _bioclip_lock:
        model, preprocess, text, torch = _load_bioclip()
        with torch.no_grad():
            feats = model.encode_image(preprocess(image.convert("RGB")).unsqueeze(0))
            feats = feats / feats.norm(dim=-1, keepdim=True)
            probs = (100.0 * feats @ text.T).softmax(dim=-1)[0].tolist()
    n = len(SPECIES)
    if max(range(len(probs)), key=probs.__getitem__) >= n:
        return {"verdict": "none", "species": []}
    organisms = sorted(range(n), key=lambda i: -probs[i])[:3]
    found = [{"name": SPECIES[i][0], "scientific": SPECIES[i][1], "group": SPECIES[i][2],
              "score": round(probs[i], 3)} for i in organisms]
    return {"verdict": "match" if found[0]["score"] >= 0.35 else "unsure", "species": found}
