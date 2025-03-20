from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, FileResponse
from pydantic import BaseModel
from typing import List, Dict
import time
import os
from google import genai
from google.genai import types
import logging
import traceback

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Check for API key at startup
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    logger.error("GOOGLE_API_KEY environment variable is not set")
    raise SystemExit("GOOGLE_API_KEY environment variable must be set to run this application")

# Validate API key on startup
try:
    client = genai.Client(api_key=GOOGLE_API_KEY)
    # Simple validation request to test the API key
    response = client.models.list()
    logger.info("API key validation successful")
except Exception as e:
    logger.error(f"Failed to validate Google API key: {str(e)}")
    raise SystemExit(f"Invalid GOOGLE_API_KEY: {str(e)}")

app = FastAPI()

# ----------------------------------
# Data Models
# ----------------------------------

class ConversionRequest(BaseModel):
    mql_query: str

class ConversionResponse(BaseModel):
    promql_query: str
    debug: Dict[str, str]
    errors: List[str] = []
    warnings: List[str] = []

# ----------------------------------
# MQL Validator
# ----------------------------------

class MQLValidator:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self, query: str) -> bool:
        self.errors = []
        self.warnings = []

        # 1. Basic empty check
        if not query.strip():
            self.errors.append("Query cannot be empty")
            return False

        # 2. Check for balanced quotes
        single_quotes = query.count("'")
        double_quotes = query.count('"')
        if single_quotes % 2 != 0:
            self.errors.append("Unmatched single quotes in query")
        if double_quotes % 2 != 0:
            self.errors.append("Unmatched double quotes in query")

        # 3. Check for balanced parentheses
        paren_count = 0
        for char in query:
            if char == '(':
                paren_count += 1
            elif char == ')':
                paren_count -= 1
            if paren_count < 0:
                self.errors.append("Unmatched closing parenthesis")
                return False
        if paren_count > 0:
            self.errors.append("Unmatched opening parenthesis")
            return False

        # 4. Basic MQL format check: must contain '|'
        parts = query.split("|")
        if len(parts) < 1:
            self.errors.append("Invalid query format (no '|' found)")
            return False

        # 5. Must start with 'fetch'
        first_part = parts[0].strip()
        if not first_part.startswith("fetch"):
            self.errors.append("Query must start with 'fetch'")
            return False

        # 6. Common pitfall check
        if "::" in query and "fetch" not in query:
            self.warnings.append("Resource specification without fetch may cause issues")

        return len(self.errors) == 0


# ----------------------------------
# Conversion Logic
# ----------------------------------

class ConversionError(Exception):
    """Custom exception for conversion errors"""
    pass

def convert_mql_to_promql(mql_query: str) -> str:
    try:
        safety_settings = [
            types.SafetySetting(category=cat, threshold="BLOCK_NONE")
            for cat in ["HARM_CATEGORY_DANGEROUS_CONTENT", "HARM_CATEGORY_HATE_SPEECH", 
                       "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_SEXUALLY_EXPLICIT"]
        ]

        system_instruction = """You are an expert at converting queries from MQL (Google Cloud Monitoring Query Language) to PromQL (Prometheus Query Language), specifically for use in Google Cloud's **Managed Service for Prometheus (MSP)**. Your overriding goal is:

1. **Convert an MQL query** into an equivalent **PromQL** query (or as close as possible).  
2. **Never** group by time-based expressions in PromQL (e.g., `by(time())`), since PromQL only allows grouping by label names.  
3. Follow all naming conventions, label conflict rules, resource labeling requirements, and known differences between MQL and PromQL, especially in the MSP environment.  

When you output your final PromQL, **do not** include any of the original MQL text. Instead, provide:

- A **single** valid PromQL query (or minimal set of queries) that replicates the MQL logic without grouping by time.  
- A short clarification **only** if needed (e.g., explaining that time-based grouping is not allowed, or noting differences in "outer join" behavior).  

### **Key Requirements and Rules**

1. **No Grouping by Time**  
   - If the MQL query (or the user) attempts to do `group_by time()`, or anything like `by(time())`, or `by(time()-time()%3600)`, you must CONVERT OR OMIT in the final PromQL.  
   - Instead, evaluate PromQL alternatives:
     - Range-vector aggregations (e.g., `sum_over_time(...)`, `increase(...)`)  
     - Using a visualization layer to bucket data by time  
     - Generating a new label that represents a time bucket externally, then grouping by that label

2. **MQL to PromQL Mapping**  
   - **`fetch`** → PromQL instant vector selector (e.g., `metric_name{...}`)  
   - **`filter`** → label matchers in curly braces (`{label="value"}`)  
   - **`group_by(...)`** → PromQL aggregation with `sum by(...)`, `avg by(...)`, etc. **Never** put a function or expression inside `by(...)`.  
   - **`join`** → binary operators in PromQL, possibly with `on()`, `ignoring()`, `group_left()`, or `group_right()`.  
   - **`align rate(...)`, `align delta(...)`** → use range-vector functions like `rate(metric[window])`, `increase(metric[window])`, etc.  
   - **`map`** → label manipulations with `label_join`, `label_replace`, or simply numeric transformations if needed.  
   - **`outer_join`** in MQL → not directly possible in PromQL. The closest is `OR` (union) or some combination of `or` + `label_replace`, but it will not preserve extra columns from both sides.  
   - **Ephemeral fields** (e.g., `is_default_value`) → PromQL cannot carry arbitrary columns. These must be turned into labels or ignored if no direct label-based approach is possible.

3. **Cloud Monitoring Metrics → PromQL Metric Names**  
   - Replace the **first** slash (`/`) with a colon (`:`).  
   - Replace **all other** special characters (`.`, additional `/`, etc.) with underscores (`_`).  
   - **Examples**:  
     - `kubernetes.io/container/cpu/limit_cores` BECOMES `kubernetes_io:container_cpu_limit_cores`  
     - `compute.googleapis.com/instance/cpu/utilization` BECOMES `compute_googleapis_com:instance_cpu_utilization`  
     - `logging.googleapis.com/log_entry_count` BECOMES `logging_googleapis_com:log_entry_count`  
     - `custom.googleapis.com/opencensus/opencensus.io/http/server/request_count_by_method`  
       BECOMES `custom_googleapis_com:opencensus_opencensus_io_http_server_request_count_by_method`  

4. **Resource Label Requirement**  
   - If a metric can map to **multiple Cloud Monitoring resource types**, you must include the label `monitored_resource="..."` in your PromQL selector. For instance:  
     ```
     compute_googleapis_com:instance_cpu_utilization{monitored_resource="gce_instance"}
     ```
   - If you omit this for multi-resource metrics, the query may fail or return an error.

5. **Distribution Metrics**  
   - For distribution-valued metrics, append `_count`, `_sum`, or `_bucket` to the name in PromQL. Example:  
     - `networking.googleapis.com/vm_flow/rtt` → `networking_googleapis_com:vm_flow_rtt_count`, `networking_googleapis_com:vm_flow_rtt_sum`, or `networking_googleapis_com:vm_flow_rtt_bucket`

6. **Label Conflicts**  
   - If a **metric label** has the same name as a **resource label**, prefix the metric label with `metric_`. E.g.:
     - Resource label: `{project_id="my-project"}`  
     - Metric label: `{metric_project_id="some-other-value"}`

7. **Handling Aligners and Windows**  
   - MQL's `align rate(1m)` or `align delta(1m)` typically becomes a range-vector function in PromQL:  
     - `rate(metric[1m])` or `increase(metric[1m])`  
   - MQL's `align sum(...)` over a window can become `sum_over_time(metric[window])`.

8. **Joins and Vector Matching**  
   - Use PromQL's binary operators (`+`, `-`, `*`, `/`, `and`, `or`, `unless`) along with `on()` or `ignoring()` to replicate MQL's `join`.  
   - If the MQL query used `outer_join`, mention that PromQL does not have a true outer join, but you can approximate with `or` or use `group_left` to keep some right-hand labels. You cannot produce extra columns or "null" fill like in SQL.

9. **No Grouping by Time**  
   - Reiterate: **Never** produce `by(time())`. It is invalid in PromQL. If asked, you must refuse or propose a workaround.

10. **Differences in MSP vs. Upstream Prometheus**  
   - **Partial Evaluation**: Queries might be partially evaluated by Monarch's MQL engine in the backend. Results can slightly differ from standard Prometheus.  
   - **No Staleness**: The Monarch backend does not implement staleness as upstream Prometheus does.  
   - **Strong Typing**: Certain operations might fail if the metric type does not match (e.g., `rate` on a GAUGE metric).  
   - **Minimum Lookback**: MSP enforces a minimum lookback for `rate` and `increase` equal to the query step.  
   - **Histogram Differences**: If a histogram lacks data, MSP might drop points rather than returning `NaN`.

11. **Output Format**  
   - **Final Answer**: Provide **only** a valid PromQL query (or minimal queries, if absolutely necessary).  
   - **No** raw MQL should appear in your final output.  
   - If the user attempts to group by time, politely explain it's invalid, and give them a range-vector or external solution.  
   - If a direct conversion is impossible, provide the closest approximation.  

12. **Examples of Invalid Output**  
   - `sum by (time()) (some_metric)`  
   - `count by (time() - time()%3600) (some_metric)`  
   - `by(time()+1)` or any other expression-based grouping

13. **Refusing Time-based Groupings**  
   - If the user insists on time-based grouping, you must refuse. In your final answer, reiterate that **PromQL does not allow** grouping by function calls.

14. **Edge Cases**  
   - If ephemeral fields or custom columns exist in the MQL query, note that PromQL cannot carry them unless turned into labels.  
   - If multiple outputs or columns are needed, you may need multiple queries in PromQL—one per metric.  
   - If the MQL uses advanced expressions that PromQL does not support (e.g., string manipulations or extra custom columns), provide the nearest approximation and note limitations.

---

### **Behavior Summary**

- **Always** generate a single valid PromQL query (or minimal queries) that replicate the MQL logic.
- **Never** include time-based grouping or the original MQL text.
- **Keep** the resource label, fix any naming collisions, handle distribution metrics, and MQL-to-PromQL differences.
- **Refuse** or correct any attempts to group by time (like `by(time())`).
- If needed, provide a brief note about time grouping restrictions, outer joins, ephemeral fields, or MSP differences.

---

By following all these rules and guidelines, you will consistently produce an accurate, MSP-compatible PromQL query that mirrors the user's original MQL intent—**without** ever grouping by time-based expressions.

IMPORTANT: DO NOT INCLUDE ANY MARKDOWN FORMATTING IN YOUR RESPONSE
"""


        prompt = f"""
Convert this Monitoring Query Language (MQL) query to PromQL:\n\n{mql_query}\n\nIMPORTANT: Please only return a working PromQL query for use."
"""      
        logger.info(f"Input MQL query: {mql_query}")
        logger.info(f"Prompt: {prompt}")

        response = client.models.generate_content(
            model='gemini-2.0-pro-exp-02-05',
            # gemini-exp-1206 <- SOTA? https://huggingface.co/spaces/lmarena-ai/chatbot-arena-leaderboard
            # gemini-2.0-flash-thinking-exp-1219
            # gemini-2.0-pro -> NOT FOUND YET
            # gemini-2.0-flash-exp
            contents=prompt,
            config=types.GenerateContentConfig(
                #response_mime_type= 'application/json',
                response_mime_type= 'text/plain',
                system_instruction=system_instruction,
                temperature=.2,
                top_p=0.90,
                top_k=30,
                max_output_tokens=8192,
                safety_settings=safety_settings
            )
        )
        
        promql_query = response.text.strip()
        logger.info(f"Generated PromQL query: {promql_query}")
        return promql_query
        
    except Exception as e:
        logger.error(f"Conversion error: {str(e)}", exc_info=True)
        raise ConversionError(f"Failed to convert query: {str(e)}")

# ----------------------------------
# Routes
# ----------------------------------

@app.get("/", response_class=HTMLResponse)
def handle_home():
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>MQL to PromQL Converter</title>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-50 min-h-screen p-4">
    <div class="max-w-3xl mx-auto">
        <div class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold">MQL to PromQL Converter</h1>
            <a href="/about" class="text-blue-600 hover:text-blue-800">About</a>
        </div>
        
        <div class="space-y-6">
            <div class="bg-white rounded-lg shadow-sm p-4">
                <div class="mb-4 text-sm text-gray-600 space-y-2">
                    <p>⚠️ Please note:</p>
                    <ul class="list-disc pl-5">
                        <li>Only MQL queries are supported as input</li>
                        <li>Running the same query multiple times may return different results due to the LLM nature</li>
                        <li>Results are valid PromQL syntax but may not work in your specific environment 😅</li>
                    </ul>
                </div>

                <div class="flex items-center justify-between mb-2">
                    <label for="mqlQuery" class="text-lg font-semibold">MQL Query:</label>
                    <button onclick="useExample()" class="text-sm text-blue-600 hover:text-blue-800">
                        Try Example
                    </button>
                </div>
                <textarea 
                    id="mqlQuery"
                    rows="6"
                    class="w-full p-3 border rounded font-mono text-sm bg-gray-50 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    placeholder="Paste your MQL query here..."></textarea>
                
                <button 
                    onclick="convertQuery()"
                    id="convertButton"
                    class="w-full mt-3 bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-4 rounded">
                    Convert to PromQL
                </button>
            </div>

            <div id="loading" class="hidden">
                <div class="bg-white rounded-lg shadow-sm p-8 text-center">
                    <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
                    <p class="text-gray-600 font-medium">Waiting for LLM...</p>
                </div>
            </div>

            <div id="result" class="hidden">
                <div class="bg-white rounded-lg shadow-sm p-4">
                    <div class="flex items-center justify-between mb-2">
                        <h3 class="text-lg font-semibold">PromQL Result:</h3>
                    </div>
                    <pre id="promqlResult" class="w-full p-3 bg-gray-50 rounded border text-sm font-mono overflow-x-auto whitespace-pre-wrap"></pre>
                </div>
            </div>

            <div id="errors" class="hidden bg-red-50 border-l-4 border-red-500 p-4 rounded">
                <h3 class="text-red-800 font-medium">Errors:</h3>
                <ul id="errorList" class="mt-1 text-red-700 text-sm"></ul>
            </div>

            <div id="warnings" class="hidden bg-yellow-50 border-l-4 border-yellow-500 p-4 rounded">
                <h3 class="text-yellow-800 font-medium">Warnings:</h3>
                <ul id="warningList" class="mt-1 text-yellow-700 text-sm"></ul>
            </div>
        </div>
    </div>

    <script>
    const exampleQueries = [
      `fetch gce_instance
| metric 'logging.googleapis.com/operation'
| filter httpRequest.status = 403
| group_by [TIMESTAMP_TRUNC(timestamp, HOUR)]
| count()`,
      `fetch https_lb_rule::loadbalancing.googleapis.com/https/request_count
| align rate(1m)
| scale "{requests}/min"
| every 1m
| group_by
    [metric.cache_result,metric.proxy_continent,
     metric.response_code_class,metric.response_code,
     metric.protocol,resource.backend_name,
     resource.backend_type,resource.backend_scope,
     resource.backend_scope_type, resource.backend_target_type,
     resource.backend_target_name, resource.forwarding_rule_name, 
     resource.matched_url_path_rule,
     resource.target_proxy_name, resource.url_map_name,
     resource.region,resource.project_id ],
    [value_request_count_aggregate:
       aggregate(val(0))]
| div 60
| within 5m
      `,
      `fetch global
| { 
    t_0:
      metric 'custom.googleapis.com/http/server/requests/count'
      | filter
        (metric.service == 'service-a' && metric.uri =~ '/api/started')
      | every (1m)
      | map drop [resource.project_id, metric.status, metric.uri, metric.exception, metric.method, metric.service, metric.outcome]
    ; t_1:
      metric 'custom.googleapis.com/http/server/requests/count'
      | filter
        (metric.service == 'service-a' && metric.uri =~ '/api/completed')
      | every (1m)
      | map drop [resource.project_id, metric.status, metric.uri, metric.exception, metric.method, metric.service, metric.outcome]
  }
| within   d'2021/11/18-00:00:00', d'2021/11/18-15:15:00'
| outer_join 0
| value val(0)-val(1)`,
      `fetch gce_instance
| metric 'compute.googleapis.com/instance/uptime'
| filter (metric.instance_name == 'instance-1')
| align delta(1d)
| every 1d
| group_by [], [value_uptime_mean: mean(value.uptime)]`
    ];

    function useExample() {
        const randomIndex = Math.floor(Math.random() * exampleQueries.length);
        const selectedQuery = exampleQueries[randomIndex];
        document.getElementById('mqlQuery').value = selectedQuery;
    }

    function copyResult() {
        const result = document.getElementById('promqlResult').textContent;
        navigator.clipboard.writeText(result).then(function() {
            const copyButton = document.getElementById('copyButton');
            copyButton.textContent = 'Copied!';
            copyButton.classList.add('bg-green-100', 'text-green-700');
            setTimeout(() => {
                copyButton.textContent = 'Copy';
                copyButton.classList.remove('bg-green-100', 'text-green-700');
            }, 2000);
        });
    }

    async function convertQuery() {
        const mqlQuery = document.getElementById('mqlQuery').value;
        const resultDiv = document.getElementById('result');
        const loadingDiv = document.getElementById('loading');
        const promqlResult = document.getElementById('promqlResult');
        const errorsDiv = document.getElementById('errors');
        const warningsDiv = document.getElementById('warnings');
        const errorList = document.getElementById('errorList');
        const warningList = document.getElementById('warningList');
        const convertButton = document.getElementById('convertButton');

        // Hide any previous results first
        resultDiv.classList.add('hidden');
        errorsDiv.classList.add('hidden');
        warningsDiv.classList.add('hidden');

        // Show loading and disable button
        loadingDiv.classList.remove('hidden');
        convertButton.disabled = true;

        try {
            const response = await fetch('/convert', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ mql_query: mqlQuery })
            });

            const data = await response.json();
            
            // Hide loading
            loadingDiv.classList.add('hidden');
            convertButton.disabled = false;

            // Show errors if any
            if (data.errors && data.errors.length > 0) {
                errorsDiv.classList.remove('hidden');
                errorList.innerHTML = data.errors.map(function(error) {
                    return '<li>' + error + '</li>';
                }).join('');
            } else {
                // Only show result if there are no errors
                resultDiv.classList.remove('hidden');
                promqlResult.textContent = data.promql_query;
            }

            // Show warnings if any
            if (data.warnings && data.warnings.length > 0) {
                warningsDiv.classList.remove('hidden');
                warningList.innerHTML = data.warnings.map(function(warning) {
                    return '<li>' + warning + '</li>';
                }).join('');
            }
        } catch (error) {
            loadingDiv.classList.add('hidden');
            convertButton.disabled = false;
            errorsDiv.classList.remove('hidden');
            errorList.innerHTML = '<li>Error: ' + error.message + '</li>';
        }
    }
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

@app.get("/about", response_class=HTMLResponse)
def handle_about():
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <title>About - MQL to PromQL Converter</title>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-50 min-h-screen p-4">
    <div class="max-w-3xl mx-auto">
        <div class="flex justify-between items-center mb-6">
            <h1 class="text-2xl sm:text-3xl font-bold">About</h1>
            <a href="/" class="text-blue-600 hover:text-blue-800">Back to Converter</a>
        </div>
        
        <div class="bg-white rounded-lg shadow-sm p-6 space-y-4">
            <p>On July 22, 2025, MQL will no longer be available for new dashboards and alerts in the Google Cloud console, and Google Cloud customer support will end. (<a href="https://cloud.google.com/stackdriver/docs/deprecations/mql">source</a>)</p>
            
            <p>This tool helps convert Monitoring Query Language (MQL) queries to PromQL format using <a href="https://gemini.google.com/app">Gemini</a>.</p>
            
            <p>While it provides syntactically correct PromQL, the actual functionality may need adjustments based on your specific Prometheus setup and available metrics.</p>
            
            <p>This is an experimental tool, and results should be carefully validated before use in production environments.</p>
        </div>
    </div>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

@app.get("/health")
def health():
    return PlainTextResponse("OK")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse("favicon.ico")

@app.post("/convert", response_model=ConversionResponse)
async def handle_convert(req: ConversionRequest, request: Request):
    try:
        validator = MQLValidator()
        is_valid = validator.validate(req.mql_query)
        
        logger.info(f"Validating MQL query: {req.mql_query}")
        if not is_valid:
            logger.warning(f"Validation failed: {validator.errors}")
            return JSONResponse(
                status_code=400,
                content={
                    "promql_query": "",
                    "debug": getattr(request.state, "debug", {}),
                    "errors": validator.errors,
                    "warnings": validator.warnings
                }
            )

        promql_query = convert_mql_to_promql(req.mql_query)
        logger.info(f"Successful conversion: {promql_query}")
        return {
            "promql_query": promql_query,
            "debug": getattr(request.state, "debug", {}),
            "errors": [],
            "warnings": validator.warnings
        }
        
    except Exception as e:
        error_details = {
            "error": str(e),
            "traceback": traceback.format_exc(),
            "request_data": req.dict(),
            "debug_info": getattr(request.state, "debug", {})
        }
        logger.error(f"Conversion error: {error_details}")
        return JSONResponse(
            status_code=500,
            content={
                "promql_query": "",
                "debug": error_details,
                "errors": [str(e)],
                "warnings": []
            }
        )
