# Helper funcs for LLM_XXXXX.py
import tiktoken, json, os, yaml
from langchain_core.output_parsers.format_instructions import JSON_FORMAT_INSTRUCTIONS
from transformers import AutoTokenizer
import GPUtil
import time
import psutil
import threading
import torch
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from vouchervision.tool_taxonomy_WFO import validate_taxonomy_WFO, WFONameMatcher
    from vouchervision.tool_geolocate_HERE import validate_coordinates_here
    from vouchervision.tool_wikipedia import validate_wikipedia
except:
    from tool_taxonomy_WFO import validate_taxonomy_WFO, WFONameMatcher
    from tool_geolocate_HERE import validate_coordinates_here
    from tool_wikipedia import validate_wikipedia

def run_tools(output, tool_WFO, tool_GEO, tool_wikipedia, json_file_path_wiki):
    # Define a function that will catch and return the results of your functions
    def task(func, *args, **kwargs):
        return func(*args, **kwargs)

    # List of tasks to run in separate threads
    tasks = [
        (validate_taxonomy_WFO, (tool_WFO, output, False)),
        (validate_coordinates_here, (tool_GEO, output, False)),
        (validate_wikipedia, (tool_wikipedia, json_file_path_wiki, output)),
    ]

    # Results storage
    results = {}

    # Use ThreadPoolExecutor to execute each function in its own thread
    with ThreadPoolExecutor() as executor:
        future_to_func = {executor.submit(task, func, *args): func.__name__ for func, args in tasks}
        for future in as_completed(future_to_func):
            func_name = future_to_func[future]
            try:
                # Collecting results
                results[func_name] = future.result()
            except Exception as exc:
                print(f'{func_name} generated an exception: {exc}')

    # Here, all threads have completed
    # Extracting results
    Matcher = WFONameMatcher(tool_WFO)
    GEO_dict_null = {
        'GEO_override_OCR': False,
        'GEO_method': '',
        'GEO_formatted_full_string': '',
        'GEO_decimal_lat': '',
        'GEO_decimal_long': '',
        'GEO_city': '',
        'GEO_county': '',
        'GEO_state': '',
        'GEO_state_code': '',
        'GEO_country': '',
        'GEO_country_code': '',
        'GEO_continent': '',
    }
    output_WFO, WFO_record = results.get('validate_taxonomy_WFO', (output, Matcher.NULL_DICT))
    output_GEO, GEO_record = results.get('validate_coordinates_here', (output, GEO_dict_null))

    return output_WFO, WFO_record, output_GEO, GEO_record



def save_individual_prompt(prompt_template, txt_file_path_ind_prompt):
    with open(txt_file_path_ind_prompt, 'w',encoding='utf-8') as file:
        file.write(prompt_template)



def sanitize_prompt(data):
    if isinstance(data, dict):
        return {sanitize_prompt(key): sanitize_prompt(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [sanitize_prompt(element) for element in data]
    elif isinstance(data, str):
        return data.encode('utf-8', 'ignore').decode('utf-8')
    else:
        return data
    

def count_tokens(string, vendor, model_name):
    full_string = string + JSON_FORMAT_INSTRUCTIONS
    
    def run_count(full_string, model_name):
            # Ensure the encoding is obtained correctly.
            encoding = tiktoken.encoding_for_model(model_name)
            tokens = encoding.encode(full_string)
            return len(tokens)
        
    try:
        if vendor == 'mistral':
            tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-v0.1")

            tokens = tokenizer.tokenize(full_string)
            return len(tokens)
        
        else:
            return run_count(full_string, model_name)
        
    except Exception as e:
        print(f"An error occurred: {e}")
        return 0
    


class SystemLoadMonitor():
    def __init__(self, logger) -> None:
        self.monitoring_thread = None
        self.logger = logger
        self.gpu_usage = {'max_cpu_usage': 0, 'max_load': 0, 'max_vram_usage': 0, "max_ram_usage": 0, 'n_gpus': 0, 'monitoring': True}
        self.start_time = None
        self.tool_start_time = None
        self.has_GPU = torch.cuda.is_available()
        self.monitor_interval = 2
        
    def start_monitoring_usage(self):
        self.start_time = time.time()
        self.monitoring_thread = threading.Thread(target=self.monitor_usage, args=(self.monitor_interval,))
        self.monitoring_thread.start()

    def stop_inference_timer(self):
        # Stop inference timer and record elapsed time
        self.inference_time = time.time() - self.start_time
        # Immediately start the tool timer
        self.tool_start_time = time.time()

    def monitor_usage(self, interval):
        while self.gpu_usage['monitoring']:
            # GPU monitoring
            if self.has_GPU:
                GPUs = GPUtil.getGPUs()
                self.gpu_usage['n_gpus'] = len(GPUs)  # Count the number of GPUs
                total_load = 0
                total_memory_usage_gb = 0
                for gpu in GPUs:
                    total_load += gpu.load
                    total_memory_usage_gb += gpu.memoryUsed / 1024.0
                
                if self.gpu_usage['n_gpus'] > 0:  # Avoid division by zero
                    # Calculate the average load and memory usage across all GPUs
                    self.gpu_usage['max_load'] = max(self.gpu_usage['max_load'], total_load / self.gpu_usage['n_gpus'])
                    self.gpu_usage['max_vram_usage'] = max(self.gpu_usage['max_vram_usage'], total_memory_usage_gb)
            
            # RAM monitoring
            ram_usage = psutil.virtual_memory().used / (1024.0 ** 3)  # Get RAM usage in GB
            self.gpu_usage['max_ram_usage'] = max(self.gpu_usage.get('max_ram_usage', 0), ram_usage)
            
            # CPU monitoring
            cpu_usage = psutil.cpu_percent(interval=None)
            self.gpu_usage['max_cpu_usage'] = max(self.gpu_usage.get('max_cpu_usage', 0), cpu_usage)
            time.sleep(interval)

    def get_current_datetime(self):
        # Get the current date and time
        now = datetime.now()
        # Format it as a string, replacing colons with underscores
        datetime_iso = now.strftime('%Y_%m_%dT%H_%M_%S')
        return datetime_iso

    def stop_monitoring_report_usage(self):
        self.gpu_usage['monitoring'] = False
        self.monitoring_thread.join()
        tool_time = time.time() - self.tool_start_time if self.tool_start_time else 0

        num_gpus, gpu_dict, total_vram_gb, capability_score = check_system_gpus()

        report = {
            'inference_time_s': str(round(self.inference_time, 2)),
            'tool_time_s': str(round(tool_time, 2)),
            'max_cpu': str(round(self.gpu_usage['max_cpu_usage'], 2)),
            'max_ram_gb': str(round(self.gpu_usage['max_ram_usage'], 2)),
            'current_time': self.get_current_datetime(),
            'n_gpus': self.gpu_usage['n_gpus'],
            'total_gpu_vram_gb':total_vram_gb, 
            'capability_score':capability_score,
            
        }

        if self.logger:
            self.logger.info(f"Inference Time: {round(self.inference_time,2)} seconds")
            self.logger.info(f"Tool Time: {round(tool_time,2)} seconds")
            self.logger.info(f"Max CPU Usage: {round(self.gpu_usage['max_cpu_usage'],2)}%")
            self.logger.info(f"Max RAM Usage: {round(self.gpu_usage['max_ram_usage'],2)}GB")
        else:
            print(f"Inference Time: {round(self.inference_time,2)} seconds")
            print(f"Tool Time: {round(tool_time,2)} seconds")
            print(f"Max CPU Usage: {round(self.gpu_usage['max_cpu_usage'],2)}%")
            print(f"Max RAM Usage: {round(self.gpu_usage['max_ram_usage'],2)}GB")

        if self.has_GPU:
            report.update({'max_gpu_load': str(round(self.gpu_usage['max_load'] * 100, 2))})
            report.update({'max_gpu_vram_gb': str(round(self.gpu_usage['max_vram_usage'], 2))})
            if self.logger:
                self.logger.info(f"Max GPU Load: {round(self.gpu_usage['max_load'] * 100, 2)}%")
                self.logger.info(f"Max GPU Memory Usage: {round(self.gpu_usage['max_vram_usage'], 2)}GB")
            else:
                print(f"Max GPU Load: {round(self.gpu_usage['max_load'] * 100, 2)}%")
                print(f"Max GPU Memory Usage: {round(self.gpu_usage['max_vram_usage'], 2)}GB")
        else:
            report.update({'max_gpu_load': '0'})
            report.update({'max_gpu_vram_gb': '0'})

        return report
    


def check_system_gpus():
    print(f"Torch CUDA: {torch.cuda.is_available()}")
    # if not torch.cuda.is_available():
    #     return 0, {}, 0, "no_gpu"
    
    GPUs = GPUtil.getGPUs()
    num_gpus = len(GPUs)
    gpu_dict = {}
    total_vram = 0
    
    for i, gpu in enumerate(GPUs):
        gpu_vram = gpu.memoryTotal  # VRAM in MB
        gpu_dict[f"GPU_{i}"] = f"{gpu_vram / 1024} GB"  # Convert to GB
        total_vram += gpu_vram
    
    total_vram_gb = total_vram / 1024  # Convert total VRAM to GB
    
    capability_score_map = {
        "no_gpu": 0,
        "class_8GB": 10,
        "class_12GB": 14,
        "class_16GB": 18,
        "class_24GB": 26,
        "class_48GB": 50,
        "class_96GB": 100,
        "class_96GBplus": float('inf'),  # Use infinity to represent any value greater than 96GB
    }
    
    # Determine the capability score based on the total VRAM
    capability_score = "no_gpu"
    for score, vram in capability_score_map.items():
        if total_vram_gb <= vram:
            capability_score = score
            break
    else:
        capability_score = "class_max"
    
    return num_gpus, gpu_dict, total_vram_gb, capability_score
    
    




if __name__ == '__main__':
    num_gpus, gpu_dict, total_vram_gb, capability_score = check_system_gpus()
    print(f"Number of GPUs: {num_gpus}")
    print(f"GPU Details: {gpu_dict}")
    print(f"Total VRAM: {total_vram_gb} GB")
    print(f"Capability Score: {capability_score}")










