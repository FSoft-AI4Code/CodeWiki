#!/usr/bin/env python3
"""
HTML templates for the CodeWiki web application.
"""

# Web interface HTML template
WEB_INTERFACE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CodeWiki - GitHub Repository Documentation Generator</title>
    <style>
        :root {
            --primary-color: #2563eb;
            --secondary-color: #f1f5f9;
            --text-color: #334155;
            --border-color: #e2e8f0;
            --success-color: #10b981;
            --warning-color: #f59e0b;
            --error-color: #ef4444;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: var(--text-color);
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }
        
        .header {
            background: var(--primary-color);
            color: white;
            padding: 2rem;
            text-align: center;
        }
        
        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 0.5rem;
            font-weight: 700;
        }
        
        .header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }
        
        .content {
            padding: 2rem;
        }
        
        .form-group {
            margin-bottom: 1.5rem;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 600;
            color: var(--text-color);
        }
        
        .form-group input {
            width: 100%;
            padding: 0.75rem 1rem;
            border: 2px solid var(--border-color);
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.2s ease;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: var(--primary-color);
        }
        
        .btn {
            display: inline-block;
            padding: 0.75rem 2rem;
            background: var(--primary-color);
            color: white;
            text-decoration: none;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 1rem;
            font-weight: 600;
            transition: all 0.2s ease;
        }
        
        .btn:hover {
            background: #1d4ed8;
            transform: translateY(-1px);
        }
        
        .btn:disabled {
            background: #94a3b8;
            cursor: not-allowed;
            transform: none;
        }
        
        .alert {
            padding: 1rem;
            border-radius: 8px;
            margin-bottom: 1rem;
        }
        
        .alert-success {
            background: #dcfce7;
            color: #166534;
            border: 1px solid #bbf7d0;
        }
        
        .alert-error {
            background: #fef2f2;
            color: #991b1b;
            border: 1px solid #fecaca;
        }
        
        .recent-jobs {
            margin-top: 2rem;
            border-top: 1px solid var(--border-color);
            padding-top: 2rem;
        }
        
        .job-item {
            background: var(--secondary-color);
            padding: 1rem;
            border-radius: 8px;
            margin-bottom: 1rem;
        }
        
        .job-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.5rem;
        }
        
        .job-url {
            font-weight: 600;
            color: var(--primary-color);
        }
        
        .job-status {
            padding: 0.25rem 0.75rem;
            border-radius: 16px;
            font-size: 0.875rem;
            font-weight: 600;
        }
        
        .status-queued {
            background: #fef3c7;
            color: #92400e;
        }
        
        .status-processing {
            background: #dbeafe;
            color: #1e40af;
        }
        
        .status-completed {
            background: #dcfce7;
            color: #166534;
        }
        
        .status-failed {
            background: #fef2f2;
            color: #991b1b;
        }
        
        .job-progress {
            font-size: 0.875rem;
            color: #64748b;
            margin-top: 0.25rem;
        }
        
        .job-actions {
            margin-top: 0.5rem;
        }
        
        .btn-small {
            padding: 0.5rem 1rem;
            font-size: 0.875rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📚 CodeWiki</h1>
            <p>Generate comprehensive documentation for any GitHub repository</p>
        </div>
        
        <div class="content">
            {% if message %}
            <div class="alert alert-{{ message_type }}">
                {{ message }}
            </div>
            {% endif %}
            
            <form method="POST" action="/">
                <div class="form-group">
                    <label for="repo_url">GitHub Repository URL:</label>
                    <input 
                        type="url" 
                        id="repo_url" 
                        name="repo_url" 
                        placeholder="https://github.com/owner/repository"
                        required
                        value="{{ repo_url or '' }}"
                    >
                </div>
                
                <div class="form-group">
                    <label for="commit_id">Commit ID (optional):</label>
                    <input 
                        type="text" 
                        id="commit_id" 
                        name="commit_id" 
                        placeholder="Enter specific commit hash (defaults to latest)"
                        value="{{ commit_id or '' }}"
                        pattern="[a-f0-9]{4,40}"
                        title="Enter a valid commit hash (4-40 characters, hexadecimal)"
                    >
                </div>
                
                <button type="submit" class="btn">Generate Documentation</button>
            </form>
            
            {% if recent_jobs %}
            <div class="recent-jobs">
                <h3>Recent Jobs</h3>
                {% for job in recent_jobs %}
                <div class="job-item">
                    <div class="job-header">
                        <div class="job-url">{{ job.repo_url }}</div>
                        <div class="job-status status-{{ job.status }}">{{ job.status }}</div>
                    </div>
                    <div class="job-progress">{{ job.progress }}</div>
                    {% if job.main_model %}
                    <div class="job-model" style="font-size: 0.75rem; color: #64748b; margin-top: 0.25rem;">
                        Generated with: {{ job.main_model }}
                    </div>
                    {% endif %}
                    <div class="job-actions">
                        <a href="/docs/{{ job.job_id }}" class="btn btn-small">View Documentation</a>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
    </div>
    
    <script>
        // Form submission protection
        let isSubmitting = false;
        
        document.addEventListener('DOMContentLoaded', function() {
            const form = document.querySelector('form');
            const submitButton = document.querySelector('button[type="submit"]');
            
            if (form && submitButton) {
                form.addEventListener('submit', function(e) {
                    if (isSubmitting) {
                        e.preventDefault();
                        return false;
                    }
                    
                    isSubmitting = true;
                    submitButton.disabled = true;
                    submitButton.textContent = 'Processing...';
                    
                    // Re-enable after 10 seconds as a failsafe
                    setTimeout(function() {
                        isSubmitting = false;
                        submitButton.disabled = false;
                        submitButton.textContent = 'Generate Documentation';
                    }, 10000);
                });
            }
            
            // Optional: Add manual refresh button instead of auto-refresh
            const refreshButton = document.createElement('button');
            refreshButton.textContent = 'Refresh Status';
            refreshButton.className = 'btn btn-small';
            refreshButton.style.marginTop = '1rem';
            refreshButton.onclick = function() {
                window.location.reload();
            };
            
            const recentJobsSection = document.querySelector('.recent-jobs');
            if (recentJobsSection) {
                recentJobsSection.appendChild(refreshButton);
            }
        });
    </script>
</body>
</html>
"""

# HTML template for the documentation pages
DOCS_VIEW_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <script src="https://cdn.jsdelivr.net/npm/mermaid@11.9.0/dist/mermaid.min.js"></script>
    <style>
        :root {
            --primary-color: #2563eb;
            --secondary-color: #f1f5f9;
            --text-color: #334155;
            --border-color: #e2e8f0;
            --hover-color: #f8fafc;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: var(--text-color);
            background-color: #ffffff;
        }
        
        .container {
            display: flex;
            min-height: 100vh;
        }
        
        .sidebar {
            width: 300px;
            background-color: var(--secondary-color);
            border-right: 1px solid var(--border-color);
            padding: 20px;
            overflow-y: auto;
            position: fixed;
            height: 100vh;
        }
        
        .content {
            flex: 1;
            margin-left: 300px;
            padding: 40px 60px;
            max-width: calc(100% - 300px);
        }
        
        .logo {
            font-size: 24px;
            font-weight: bold;
            color: var(--primary-color);
            margin-bottom: 30px;
            text-decoration: none;
        }
        
        .nav-section {
            margin-bottom: 25px;
        }
        
        .nav-section h3 {
            font-size: 14px;
            font-weight: 600;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 10px;
        }
        
        .nav-item {
            display: block;
            padding: 8px 12px;
            color: var(--text-color);
            text-decoration: none;
            border-radius: 6px;
            font-size: 14px;
            transition: all 0.2s ease;
            margin-bottom: 2px;
        }
        
        .nav-item:hover {
            background-color: var(--hover-color);
            color: var(--primary-color);
        }
        
        .nav-item.active {
            background-color: var(--primary-color);
            color: white;
        }
        
        .nav-subsection {
            margin-left: 15px;
            margin-top: 8px;
        }
        
        .nav-subsection .nav-item {
            font-size: 13px;
            color: #64748b;
        }
        
        .nav-section-header {
            font-size: 14px;
            font-weight: 600;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 10px;
            padding: 8px 12px;
        }
        
        /* Nested subsection indentation - scalable for any depth */
        .nav-subsection .nav-subsection {
            margin-left: 20px;
        }
        
        .nav-subsection .nav-subsection .nav-item {
            font-size: 12px;
        }
        
        /* Additional nesting levels */
        .nav-subsection .nav-subsection .nav-subsection {
            margin-left: 15px;
        }
        
        .nav-subsection .nav-subsection .nav-subsection .nav-item {
            font-size: 11px;
        }
        
        .markdown-content {
            max-width: none;
        }
        
        .markdown-content h1 {
            font-size: 2.5rem;
            font-weight: 700;
            color: #1e293b;
            margin-bottom: 1rem;
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 0.5rem;
        }
        
        .markdown-content h2 {
            font-size: 2rem;
            font-weight: 600;
            color: #334155;
            margin-top: 2rem;
            margin-bottom: 1rem;
        }
        
        .markdown-content h3 {
            font-size: 1.5rem;
            font-weight: 600;
            color: #475569;
            margin-top: 1.5rem;
            margin-bottom: 0.75rem;
        }
        
        .markdown-content p {
            margin-bottom: 1rem;
            color: #475569;
        }
        
        .markdown-content ul, .markdown-content ol {
            margin-bottom: 1rem;
            padding-left: 1.5rem;
        }
        
        .markdown-content li {
            margin-bottom: 0.5rem;
            color: #475569;
        }
        
        .markdown-content code {
            background-color: #f1f5f9;
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            font-family: 'Fira Code', 'Consolas', monospace;
            font-size: 0.875rem;
        }
        
        .markdown-content pre {
            background-color: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 0.5rem;
            padding: 1rem;
            overflow-x: auto;
            margin-bottom: 1rem;
        }
        
        .markdown-content pre code {
            background-color: transparent;
            padding: 0;
        }
        
        .markdown-content blockquote {
            border-left: 4px solid var(--primary-color);
            padding-left: 1rem;
            margin-bottom: 1rem;
            font-style: italic;
            color: #64748b;
        }
        
        .markdown-content table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 1rem;
        }
        
        .markdown-content th, .markdown-content td {
            border: 1px solid var(--border-color);
            padding: 0.75rem;
            text-align: left;
        }
        
        .markdown-content th {
            background-color: var(--secondary-color);
            font-weight: 600;
        }
        
        .markdown-content a {
            color: var(--primary-color);
            text-decoration: underline;
        }
        
        .markdown-content a:hover {
            text-decoration: none;
        }
        
        @media (max-width: 768px) {
            .sidebar {
                width: 100%;
                position: relative;
                height: auto;
            }
            
            .content {
                margin-left: 0;
                padding: 20px;
                max-width: 100%;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <nav class="sidebar">
            <a href="/static-docs/{{ job_id }}/overview.md" class="logo">📚 {{ repo_name }}</a>
            
            {% if metadata and metadata.generation_info %}
            <div style="margin: 20px 0; padding: 15px; background: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0;">
                <h4 style="margin: 0 0 10px 0; font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em;">Generation Info</h4>
                <div style="font-size: 11px; color: #475569; line-height: 1.4;">
                    <div style="margin-bottom: 4px;"><strong>Model:</strong> {{ metadata.generation_info.main_model }}</div>
                    <div style="margin-bottom: 4px;"><strong>Generated:</strong> {{ metadata.generation_info.timestamp[:16] }}</div>
                    {% if metadata.generation_info.commit_id %}
                    <div style="margin-bottom: 4px;"><strong>Commit:</strong> {{ metadata.generation_info.commit_id[:8] }}</div>
                    {% endif %}
                    {% if metadata.statistics %}
                    <div><strong>Components:</strong> {{ metadata.statistics.total_components }}</div>
                    {% endif %}
                </div>
            </div>
            {% endif %}
            
            {% if navigation %}
            <div class="nav-section">
                <a href="/static-docs/{{ job_id }}/overview.md" class="nav-item {% if current_page == 'overview.md' %}active{% endif %}">
                    Overview
                </a>
            </div>
            
            {% macro render_nav_item(key, data, depth=0) %}
                {% set indent_class = 'nav-subsection' if depth > 0 else '' %}
                {% set indent_style = 'margin-left: ' + (depth * 15)|string + 'px;' if depth > 0 else '' %}
                <div class="{{ indent_class }}" {% if indent_style %}style="{{ indent_style }}"{% endif %}>
                    {% if data.components %}
                        <a href="/static-docs/{{ job_id }}/{{ key }}.md" class="nav-item {% if current_page == key + '.md' %}active{% endif %}">
                            {{ key.replace('_', ' ').title() }}
                        </a>
                    {% else %}
                        <div class="nav-section-header" {% if depth > 0 %}style="font-size: {{ 14 - (depth * 1) }}px; text-transform: none;"{% endif %}>
                            {{ key.replace('_', ' ').title() }}
                        </div>
                    {% endif %}
                    
                    {% if data.children %}
                        {% for child_key, child_data in data.children.items() %}
                            {{ render_nav_item(child_key, child_data, depth + 1) }}
                        {% endfor %}
                    {% endif %}
                </div>
            {% endmacro %}
            
            {% for section_key, section_data in navigation.items() %}
            <div class="nav-section">
                {{ render_nav_item(section_key, section_data) }}
            </div>
            {% endfor %}
            {% endif %}
        </nav>
        
        <main class="content">
            <div class="markdown-content">
                {{ content | safe }}
            </div>
        </main>
    </div>
    
    <script>
        // Initialize mermaid with configuration
        mermaid.initialize({
            startOnLoad: true,
            theme: 'default',
            themeVariables: {
                primaryColor: '#2563eb',
                primaryTextColor: '#334155',
                primaryBorderColor: '#e2e8f0',
                lineColor: '#64748b',
                sectionBkgColor: '#f8fafc',
                altSectionBkgColor: '#f1f5f9',
                gridColor: '#e2e8f0',
                secondaryColor: '#f1f5f9',
                tertiaryColor: '#f8fafc'
            },
            flowchart: {
                htmlLabels: true,
                curve: 'basis'
            },
            sequence: {
                diagramMarginX: 50,
                diagramMarginY: 10,
                actorMargin: 50,
                width: 150,
                height: 65,
                boxMargin: 10,
                boxTextMargin: 5,
                noteMargin: 10,
                messageMargin: 35,
                mirrorActors: true,
                bottomMarginAdj: 1,
                useMaxWidth: true,
                rightAngles: false,
                showSequenceNumbers: false
            }
        });
        
        // Re-render mermaid diagrams after page load
        document.addEventListener('DOMContentLoaded', function() {
            mermaid.init(undefined, document.querySelectorAll('.mermaid'));
        });
    </script>
</body>
</html>
"""
