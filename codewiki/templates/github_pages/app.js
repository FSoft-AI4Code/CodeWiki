// CodeWiki GitHub Pages Viewer Application

(function() {
    'use strict';
    
    // Simple markdown parser (GitHub-flavored)
    function parseMarkdown(markdown) {
        let html = markdown;
        
        // Escape HTML
        html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        
        // Headers
        html = html.replace(/^##### (.*$)/gim, '<h5>$1</h5>');
        html = html.replace(/^#### (.*$)/gim, '<h4>$1</h4>');
        html = html.replace(/^### (.*$)/gim, '<h3>$1</h3>');
        html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
        html = html.replace(/^# (.*$)/gim, '<h1>$1</h1>');
        
        // Bold
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
        
        // Italic
        html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
        html = html.replace(/_(.+?)_/g, '<em>$1</em>');
        
        // Code blocks with language support (including mermaid)
        html = html.replace(/```(\w+)?\n([\s\S]*?)```/g, function(match, lang, code) {
            code = code.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
            if (lang === 'mermaid') {
                return '<div class="mermaid">' + code + '</div>';
            }
            return '<pre><code class="language-' + (lang || 'plaintext') + '">' + code.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</code></pre>';
        });
        
        // Inline code
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        
        // Links
        html = html.replace(/\[([^\]]+)\]\(([^\)]+)\)/g, '<a href="$2">$1</a>');
        
        // Images
        html = html.replace(/!\[([^\]]*)\]\(([^\)]+)\)/g, '<img src="$2" alt="$1" />');
        
        // Blockquotes
        html = html.replace(/^\> (.+)$/gim, '<blockquote>$1</blockquote>');
        
        // Horizontal rules
        html = html.replace(/^---$/gim, '<hr>');
        html = html.replace(/^\*\*\*$/gim, '<hr>');
        
        // Unordered lists
        html = html.replace(/^\* (.+)$/gim, '<li>$1</li>');
        html = html.replace(/^- (.+)$/gim, '<li>$1</li>');
        html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
        
        // Ordered lists
        html = html.replace(/^\d+\. (.+)$/gim, '<li>$1</li>');
        
        // Tables (basic support)
        html = html.replace(/\|(.+)\|/g, function(match) {
            const cells = match.split('|').filter(cell => cell.trim());
            return '<tr>' + cells.map(cell => '<td>' + cell.trim() + '</td>').join('') + '</tr>';
        });
        html = html.replace(/(<tr>.*<\/tr>)/s, '<table>$1</table>');
        
        // Paragraphs
        html = html.replace(/\n\n/g, '</p><p>');
        html = '<p>' + html + '</p>';
        
        // Clean up
        html = html.replace(/<p><\/p>/g, '');
        html = html.replace(/<p>(<h[1-6]>)/g, '$1');
        html = html.replace(/(<\/h[1-6]>)<\/p>/g, '$1');
        html = html.replace(/<p>(<table>)/g, '$1');
        html = html.replace(/(<\/table>)<\/p>/g, '$1');
        html = html.replace(/<p>(<ul>)/g, '$1');
        html = html.replace(/(<\/ul>)<\/p>/g, '$1');
        html = html.replace(/<p>(<pre>)/g, '$1');
        html = html.replace(/(<\/pre>)<\/p>/g, '$1');
        html = html.replace(/<p>(<div class="mermaid">)/g, '$1');
        html = html.replace(/(<\/div>)<\/p>/g, '$1');
        html = html.replace(/<p>(<hr>)<\/p>/g, '$1');
        html = html.replace(/<p>(<blockquote>)/g, '$1');
        html = html.replace(/(<\/blockquote>)<\/p>/g, '$1');
        
        return html;
    }
    
    // Initialize mermaid
    mermaid.initialize({
        startOnLoad: false,
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
    
    // Router
    class Router {
        constructor() {
            this.routes = {};
            this.currentRoute = null;
            window.addEventListener('hashchange', () => this.handleRoute());
        }
        
        handleRoute() {
            const hash = window.location.hash.slice(1) || '/';
            const cleanHash = hash.startsWith('/') ? hash.slice(1) : hash;
            const path = (hash === '/' || cleanHash === '') ? 'overview.md' : cleanHash;
            this.loadMarkdown(path);
        }
        
        async loadMarkdown(filename) {
            const content = document.getElementById('markdown-content');
            
            // Show loading state
            content.innerHTML = '<div class="loading">Loading...</div>';
            
            try {
                const response = await fetch(filename);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
                const markdown = await response.text();
                this.renderMarkdown(markdown);
                this.updateActiveLink(filename);
            } catch (error) {
                console.error('Error loading markdown:', error);
                content.innerHTML = `
                    <h1>📄 Document Not Found</h1>
                    <p>Could not load <code>${filename}</code></p>
                    <p style="color: #64748b; font-size: 0.9rem;">
                        ${error.message}
                    </p>
                    <p>
                        <a href="#/">← Back to Overview</a>
                    </p>
                `;
            }
        }
        
        updateActiveLink(filename) {
            document.querySelectorAll('.nav-link').forEach(link => {
                link.classList.remove('active');
                const linkPath = link.getAttribute('href').replace('#/', '').replace('#', '');
                const currentPath = filename;
                if (linkPath === currentPath || (linkPath === '' && filename === 'overview.md')) {
                    link.classList.add('active');
                }
            });
        }
        
        async renderMarkdown(markdown) {
            const html = parseMarkdown(markdown);
            const content = document.getElementById('markdown-content');
            content.innerHTML = html;
            
            // Process mermaid diagrams
            const mermaidDivs = content.querySelectorAll('.mermaid');
            if (mermaidDivs.length > 0) {
                try {
                    await mermaid.run({
                        nodes: mermaidDivs
                    });
                } catch (e) {
                    console.error('Mermaid rendering error:', e);
                }
            }
            
            // Scroll to top
            window.scrollTo(0, 0);
        }
    }
    
    // Navigation tree renderer with proper subsection support
    function renderNavTree(tree, container, depth = 0) {
        if (!tree || typeof tree !== 'object') return;
        
        for (const [moduleName, moduleData] of Object.entries(tree)) {
            if (moduleName === 'description' || moduleName === 'components') continue;
            
            // Check if this has components (is a page) or just children (is a section)
            const hasComponents = moduleData && moduleData.components && moduleData.components.length > 0;
            const hasChildren = moduleData && moduleData.children && Object.keys(moduleData.children).length > 0;
            
            if (hasComponents) {
                // This is a page - render as link
                const item = document.createElement('div');
                item.className = depth > 0 ? 'nav-subsection' : 'nav-item';
                
                const link = document.createElement('a');
                link.href = `#/${moduleName}.md`;
                link.textContent = formatModuleName(moduleName);
                link.className = 'nav-link';
                
                item.appendChild(link);
                container.appendChild(item);
            } else if (hasChildren) {
                // This is a section header - render as text
                const sectionHeader = document.createElement('div');
                sectionHeader.className = 'nav-section-header';
                sectionHeader.textContent = formatModuleName(moduleName);
                if (depth > 0) {
                    sectionHeader.style.fontSize = `${14 - (depth * 1)}px`;
                    sectionHeader.style.textTransform = 'none';
                }
                container.appendChild(sectionHeader);
            }
            
            // Render children recursively
            if (hasChildren) {
                const subsection = document.createElement('div');
                subsection.className = 'nav-subsection';
                container.appendChild(subsection);
                renderNavTree(moduleData.children, subsection, depth + 1);
            }
        }
    }
    
    // Format module name for display
    function formatModuleName(name) {
        return name
            .replace(/[_-]/g, ' ')
            .split(' ')
            .map(word => word.charAt(0).toUpperCase() + word.slice(1))
            .join(' ');
    }
    
    // Render metadata info box
    function renderMetadata(metadata) {
        if (!metadata || !metadata.generation_info) return null;
        
        const metadataBox = document.createElement('div');
        metadataBox.style.cssText = `
            margin: 20px 0;
            padding: 15px;
            background: #f8fafc;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
        `;
        
        const header = document.createElement('h4');
        header.textContent = 'GENERATION INFO';
        header.style.cssText = `
            margin: 0 0 10px 0;
            font-size: 12px;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        `;
        metadataBox.appendChild(header);
        
        const info = metadata.generation_info;
        const contentDiv = document.createElement('div');
        contentDiv.style.cssText = 'font-size: 11px; color: #475569; line-height: 1.4;';
        
        let html = '';
        if (info.main_model) {
            html += `<div style="margin-bottom: 4px;"><strong>Model:</strong> ${info.main_model}</div>`;
        }
        if (info.timestamp) {
            const date = new Date(info.timestamp);
            html += `<div style="margin-bottom: 4px;"><strong>Generated:</strong> ${date.toLocaleString().substring(0, 16)}</div>`;
        }
        if (info.commit_id) {
            html += `<div style="margin-bottom: 4px;"><strong>Commit:</strong> ${info.commit_id.substring(0, 8)}</div>`;
        }
        if (metadata.statistics && metadata.statistics.total_components) {
            html += `<div><strong>Components:</strong> ${metadata.statistics.total_components}</div>`;
        }
        
        contentDiv.innerHTML = html;
        metadataBox.appendChild(contentDiv);
        
        return metadataBox;
    }
    
    // Initialize application
    function init() {
        const navTree = document.getElementById('nav-tree');
        
        if (MODULE_TREE) {
            // Add overview link
            const navSection = document.createElement('div');
            navSection.className = 'nav-section';
            
            const overviewItem = document.createElement('div');
            overviewItem.className = 'nav-item';
            const overviewLink = document.createElement('a');
            overviewLink.href = '#/';
            overviewLink.textContent = 'Overview';
            overviewLink.className = 'nav-link';
            overviewItem.appendChild(overviewLink);
            navSection.appendChild(overviewItem);
            navTree.appendChild(navSection);
            
            // Add metadata info box if available
            if (CODEWIKI_CONFIG && CODEWIKI_CONFIG.metadata) {
                const metadataBox = renderMetadata(CODEWIKI_CONFIG.metadata);
                if (metadataBox) {
                    document.getElementById('metadata-info').appendChild(metadataBox);
                }
            }
            
            // Render module tree
            const moduleSection = document.createElement('div');
            moduleSection.className = 'nav-section';
            navTree.appendChild(moduleSection);
            renderNavTree(MODULE_TREE, moduleSection);
        }
        
        // Initialize router
        const router = new Router();
        
        // Initial load
        router.handleRoute();
    }
    
    // Start application when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

