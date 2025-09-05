```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_web_app.py
```

Check networking for Mermaid validator
```python
import mermaid as md

error_mermaid = """
graph TB
    subgraph "Core Agent System"
        A[DefaultAgent] --> B[Agent Execution]
        B --> C[Step Processing]
        C --> D[Action Execution]
    end
    
    subgraph "Hook System"
        E[CombinedAgentHook] --> F[Hook 1]
        E --> G[Hook 2]
        E --> H[Hook N]
    end
    
    subgraph "Event Flow"
        I[on_init] --> J[on_run_start]
        J --> K[on_step_start]
        K --> L[on_actions_generated]
        L --> M[on_action_started]
        M --> N[on_action_executed]
        N --> O[on_step_done]
        O --> P{More Steps?}
        P -->|Yes| K
        P -->|No| Q[on_run_done]
    end
    
    A -.-> E : uses hooks
    B -.-> I
    C -.-> K
    D -.-> M
    
    style E fill:#e1f5fe
    style A fill:#f3e5f5
"""

true_mermaid = """
graph TB
    subgraph "CLI Configuration Module"
        A[Main CLI Entry Point<br/>sweagent.run.run.main] --> B[Command Router]
        C[BasicCLI Configuration System<br/>sweagent.run.common.BasicCLI] --> D[Config Validation]
        
        B --> E[Execution Commands]
        B --> F[Analysis Commands]
        B --> G[Inspector Commands]
        
        E --> H[Single Execution<br/>run]
        E --> I[Batch Execution<br/>run-batch]
        E --> J[Replay Execution<br/>run-replay]
        E --> K[Shell Execution<br/>shell]
        
        F --> L[Merge Predictions<br/>merge-preds]
        F --> M[Extract Predictions<br/>extract-pred]
        F --> N[Compare Runs<br/>compare-runs]
        F --> O[Quick Stats<br/>quick-stats]
        F --> P[Remove Unfinished<br/>remove-unfinished]
        
        G --> Q[Terminal Inspector<br/>inspect]
        G --> R[Web Inspector<br/>inspector]
    end
    
    subgraph "External Dependencies"
        S[execution_engines.md]
        T[inspector_tools.md]
        U[analysis_utilities.md]
    end
    
    H --> S
    I --> S
    J --> S
    K --> S
    Q --> T
    R --> T
    L --> U
    M --> U
    N --> U
    O --> U
    P --> U
    
    style A fill:#e1f5fe
    style C fill:#e8f5e8
    style B fill:#fff3e0
"""

render = md.Mermaid(error_mermaid)
print(render.svg_response.text[:50])
# => Parse error on line 26:\n...  end    A -.-> E : use

render = md.Mermaid(true_mermaid)
print(render.svg_response.text[:50])
# => <svg id="mermaid-svg" width="100%" xmlns="http://w
```