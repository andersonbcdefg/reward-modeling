import re
import torch
import matplotlib.pyplot as plt
from datasets import load_dataset, concatenate_datasets

def process_shp(example):
    if example['labels'] == 1:
        preferred = example['human_ref_A']
        dispreferred = example['human_ref_B']
    else:
        preferred = example['human_ref_B']
        dispreferred = example['human_ref_A']
    return {
        "prompt": example["history"],
        "preferred": preferred,
        "dispreferred": dispreferred,
    }

def split_by_prefix(A, B):
    shared_prefix = ""
    for i in range(min(len(A), len(B))):
        if A[i] != B[i]:
            break
        shared_prefix += A[i]
        
    A_suffix = A[len(shared_prefix):]
    B_suffix = B[len(shared_prefix):]
    
    return (shared_prefix, A_suffix, B_suffix)

def split_by_assistant(s):
    split_string = "Assistant:"
    index = s.rfind(split_string)
    if index == -1:
        return (s, "")
    else:
        prefix = s[:index+len(split_string)]
        suffix = s[index+len(split_string):]
        return (prefix, suffix)


def process_anthropic(example):
    # find longest shared prefix
    shared_prefix, chosen_suffix, rejected_suffix = split_by_prefix(example["chosen"], example["rejected"])
    
    # split off the part of the prompt up to and including the last "Assistant:"
    prompt, shared_suffix = split_by_assistant(shared_prefix)

    return {
        "prompt": prompt,
        "preferred": shared_suffix + chosen_suffix,
        "dispreferred": shared_suffix + rejected_suffix,
    }

def process_webgpt(example):
    # Define the regex pattern
    pattern = r'\[\d{1,2}(?:,\s*\d{1,2})*\]'
    answer_1 = example['answer_1']
    answer_0 = example['answer_0']
    # print(answer_1, "||", answer_0)

    prompt = example['question']['full_text']
    if example['score_0'] > 0:
        preferred = answer_0
        dispreferred = answer_1
    elif example['score_1'] > 0:
        preferred = answer_1
        dispreferred = answer_0
    else:
        preferred = ""
        dispreferred = ""
    if not isinstance(preferred, str):
        print(preferred)
        # preferred = ""
    if not isinstance(dispreferred, str):
        print(dispreferred)
        # dispreferred = ""
    return {
        "prompt": prompt,
        "preferred": re.sub(pattern, '', preferred),
        "dispreferred": re.sub(pattern, '', dispreferred)
    }

def get_combined_datasets():
    # process all datasets to have the same 3 columns: prompt, preferred, dispreferred
    shp_dataset = load_dataset("stanfordnlp/SHP", split="train")
    shp_dataset = shp_dataset.map(
        process_shp, remove_columns=shp_dataset.column_names
    )

    hh_dataset = load_dataset("Anthropic/hh-rlhf", split="train")
    hh_dataset = hh_dataset.map(
        process_anthropic, remove_columns=hh_dataset.column_names
    )

    webgpt_dataset = load_dataset("openai/webgpt_comparisons", split="train")
    webgpt_dataset = webgpt_dataset.map(
        process_webgpt, remove_columns=webgpt_dataset.column_names
    ).filter(
        lambda example: example["preferred"] != "" and example["dispreferred"] != ""
    )

    # total_samples = len(shp_dataset) + len(hh_dataset) + len(webgpt_dataset)
    
    combined = concatenate_datasets([shp_dataset, hh_dataset, webgpt_dataset]).shuffle(seed=42).flatten_indices()
    combined = combined.map(
        lambda example: {
            "length": len(example["prompt"]) + len(example["preferred"]) + len(example["dispreferred"]),
        }
    ).filter(
        lambda example: example["length"] < 10000 # characters, not tokens. 10000ch ~= 2500 tokens
    )

    median = sorted(combined["length"])[len(combined) // 2]
    print("median length:", median)

    # this splits the dataset into two parts: long and short. we save computation on the short examples by not wasting padding.
    # at training time, to avoid the model swinging wildly back and forth, the resulting dataloaders should be interleaved
    # e.g. using utilities from itertools (i think this should work)
    long = combined.filter(lambda example: example["length"] > 1250)
    short = combined.filter(lambda example: example["length"] <= 1250)

    return {
        "long": long,
        "short": short,
    }

def tokenize_function(examples, tokenizer, max_len):
    preferred_input_ids = torch.full((len(examples), max_len), tokenizer.pad_token_id, dtype=torch.long)
    dispreferred_input_ids = torch.full((len(examples), max_len), tokenizer.pad_token_id, dtype=torch.long)
    preferred_attention_masks = torch.zeros((len(examples), max_len), dtype=torch.long)
    dispreferred_attention_masks = torch.zeros((len(examples), max_len), dtype=torch.long)
    for i, (prompt, preferred, dispreferred) in enumerate(zip(examples['prompt'], examples['preferred'], examples['dispreferred'])):
        prompt_tokenized = tokenizer.encode(prompt, add_special_tokens=True, return_tensors="pt").view(-1) # prompt will have CLS and end with SEP
        
        preferred_tokenized = tokenizer.encode(preferred, add_special_tokens=False, return_tensors="pt").view(-1) # response should not have CLS, it continues the prompt
        prompt_with_preferred = torch.cat((prompt_tokenized, preferred_tokenized))[:max_len]
        preferred_mask = torch.cat((torch.ones(len(prompt_with_preferred)), torch.zeros(max_len - len(prompt_with_preferred))))
        preferred_input_ids[i, :len(prompt_with_preferred)] = prompt_with_preferred
        preferred_attention_masks[i, :] = preferred_mask

        dispreferred_tokenized = tokenizer.encode(dispreferred, add_special_tokens=False, return_tensors="pt").view(-1) 
        prompt_with_dispreferred = torch.cat((prompt_tokenized, dispreferred_tokenized))[:max_len]
        dispreferred_mask = torch.cat((torch.ones(len(prompt_with_dispreferred)), torch.zeros(max_len - len(prompt_with_dispreferred))))
        dispreferred_input_ids[i, :len(prompt_with_dispreferred)] = prompt_with_dispreferred
        dispreferred_attention_masks[i, :] = dispreferred_mask

    return {
        "preferred_input_ids": preferred_input_ids,
        "preferred_attention_masks": preferred_attention_masks,
        "dispreferred_input_ids": dispreferred_input_ids,
        "dispreferred_attention_masks": dispreferred_attention_masks
    }
