#!/bin/bash

# Docker Image Build Script for CodeWiki
# This script builds the Docker image with proper version tagging

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
IMAGE_NAME="codewiki"
DEFAULT_VERSION="latest"
PLATFORM="linux/amd64"
DOCKERFILE="docker/Dockerfile"

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}CodeWiki Docker Image Builder${NC}"
echo -e "${BLUE}================================${NC}"
echo ""

# Interactive version input
if [ -z "$1" ]; then
    echo -e "${YELLOW}请输入镜像版本号 [默认: ${DEFAULT_VERSION}]: ${NC}"
    read -r VERSION
    VERSION="${VERSION:-$DEFAULT_VERSION}"
else
    VERSION="$1"
fi

# Interactive tag input
if [ -z "$2" ]; then
    echo -e "${YELLOW}请输入镜像标签 [默认: ${VERSION}]: ${NC}"
    read -r TAG
    TAG="${TAG:-$VERSION}"
else
    TAG="$2"
fi

echo ""

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo -e "${YELLOW}项目根目录: ${PROJECT_ROOT}${NC}"
echo -e "${YELLOW}镜像名称: ${IMAGE_NAME}${NC}"
echo -e "${YELLOW}版本: ${VERSION}${NC}"
echo -e "${YELLOW}标签: ${TAG}${NC}"
echo -e "${YELLOW}平台: ${PLATFORM}${NC}"
echo ""

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo -e "${RED}错误: Docker 未安装或不在 PATH 中${NC}"
    exit 1
fi

# Check if Dockerfile exists
if [ ! -f "$PROJECT_ROOT/$DOCKERFILE" ]; then
    echo -e "${RED}错误: Dockerfile 不存在于 $PROJECT_ROOT/$DOCKERFILE${NC}"
    exit 1
fi

# Check if required files exist
echo -e "${BLUE}检查必需文件...${NC}"
REQUIRED_FILES=(
    "requirements.txt"
    "pyproject.toml"
    "README.md"
    "codewiki/__init__.py"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$PROJECT_ROOT/$file" ] && [ ! -d "$PROJECT_ROOT/$file" ]; then
        echo -e "${RED}错误: 必需文件不存在: $file${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ $file${NC}"
done
echo ""

# Change to project root directory
cd "$PROJECT_ROOT"

# Build the Docker image
echo -e "${BLUE}开始构建 Docker 镜像...${NC}"
echo ""

BUILD_ARGS=(
    --platform "$PLATFORM"
    --file "$DOCKERFILE"
    --tag "${IMAGE_NAME}:${VERSION}"
    --tag "${IMAGE_NAME}:${TAG}"
)

# Add build progress output
if docker build "${BUILD_ARGS[@]}" .; then
    echo ""
    echo -e "${GREEN}================================${NC}"
    echo -e "${GREEN}镜像构建成功!${NC}"
    echo -e "${GREEN}================================${NC}"
    echo ""
    echo -e "${GREEN}已创建镜像:${NC}"
    echo -e "  ${BLUE}${IMAGE_NAME}:${VERSION}${NC}"
    echo -e "  ${BLUE}${IMAGE_NAME}:${TAG}${NC}"
    echo ""
    
    # Show image details
    echo -e "${BLUE}镜像信息:${NC}"
    docker images "${IMAGE_NAME}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"
    echo ""
    
    # Show usage instructions
    echo -e "${YELLOW}使用方法:${NC}"
    echo -e "  1. 使用 docker-compose 运行:"
    echo -e "     ${BLUE}docker-compose -f docker/docker-compose.yml up -d${NC}"
    echo ""
    echo -e "  2. 直接运行容器:"
    echo -e "     ${BLUE}docker run -d -p 8000:8000 --name codewiki ${IMAGE_NAME}:${VERSION}${NC}"
    echo ""
    echo -e "  3. 导出镜像:"
    echo -e "     ${BLUE}docker save ${IMAGE_NAME}:${VERSION} -o codewiki-${VERSION}.tar${NC}"
    echo ""
    
    # Ask if user wants to push to Docker Hub
    echo -e "${YELLOW}是否推送镜像到 Docker Hub? (y/N): ${NC}"
    read -r PUSH_CONFIRM
    
    if [[ "$PUSH_CONFIRM" =~ ^[Yy]$ ]]; then
        echo ""
        echo -e "${BLUE}准备推送镜像到 Docker Hub...${NC}"
        
        # Tag for Docker Hub
        DOCKER_HUB_REPO="xujialiang/codewiki"
        
        echo -e "${BLUE}打标签: ${DOCKER_HUB_REPO}:${VERSION}${NC}"
        if docker tag "${IMAGE_NAME}:${VERSION}" "${DOCKER_HUB_REPO}:${VERSION}"; then
            echo -e "${GREEN}✓ 标签创建成功${NC}"
        else
            echo -e "${RED}✗ 标签创建失败${NC}"
            exit 1
        fi
        
        if [ "${TAG}" != "${VERSION}" ]; then
            echo -e "${BLUE}打标签: ${DOCKER_HUB_REPO}:${TAG}${NC}"
            if docker tag "${IMAGE_NAME}:${VERSION}" "${DOCKER_HUB_REPO}:${TAG}"; then
                echo -e "${GREEN}✓ 标签创建成功${NC}"
            else
                echo -e "${RED}✗ 标签创建失败${NC}"
                exit 1
            fi
        fi
        
        echo ""
        echo -e "${BLUE}推送镜像: ${DOCKER_HUB_REPO}:${VERSION}${NC}"
        if docker push "${DOCKER_HUB_REPO}:${VERSION}"; then
            echo -e "${GREEN}✓ ${DOCKER_HUB_REPO}:${VERSION} 推送成功${NC}"
        else
            echo -e "${RED}✗ 推送失败，请确保已登录 Docker Hub (docker login)${NC}"
            exit 1
        fi
        
        if [ "${TAG}" != "${VERSION}" ]; then
            echo -e "${BLUE}推送镜像: ${DOCKER_HUB_REPO}:${TAG}${NC}"
            if docker push "${DOCKER_HUB_REPO}:${TAG}"; then
                echo -e "${GREEN}✓ ${DOCKER_HUB_REPO}:${TAG} 推送成功${NC}"
            else
                echo -e "${RED}✗ 推送失败${NC}"
                exit 1
            fi
        fi
        
        echo ""
        echo -e "${GREEN}================================${NC}"
        echo -e "${GREEN}镜像推送完成!${NC}"
        echo -e "${GREEN}================================${NC}"
        echo ""
        echo -e "${GREEN}已推送镜像:${NC}"
        echo -e "  ${BLUE}${DOCKER_HUB_REPO}:${VERSION}${NC}"
        if [ "${TAG}" != "${VERSION}" ]; then
            echo -e "  ${BLUE}${DOCKER_HUB_REPO}:${TAG}${NC}"
        fi
        echo ""
        echo -e "${YELLOW}拉取镜像:${NC}"
        echo -e "  ${BLUE}docker pull ${DOCKER_HUB_REPO}:${VERSION}${NC}"
        echo ""
    else
        echo ""
        echo -e "${YELLOW}跳过推送。如需稍后推送，请执行:${NC}"
        echo -e "  ${BLUE}docker tag ${IMAGE_NAME}:${VERSION} xujialiang/codewiki:${VERSION}${NC}"
        echo -e "  ${BLUE}docker push xujialiang/codewiki:${VERSION}${NC}"
        echo ""
    fi
    
else
    echo ""
    echo -e "${RED}================================${NC}"
    echo -e "${RED}镜像构建失败!${NC}"
    echo -e "${RED}================================${NC}"
    exit 1
fi
