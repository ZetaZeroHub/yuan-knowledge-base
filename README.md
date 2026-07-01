# Yuan Knowledge Base

[中文版本](README.zh-CN.md)

A personalized knowledge workspace for researching materials, reviewing interview fundamentals, and practicing mock interviews.

## Overview

**Yuan Knowledge Base** is designed to help you build a truly personal knowledge base. It systematically brings together information scattered across different sources and gradually turns it into your own knowledge and skills through learning, questioning, review, and mock interviews.

---

## Cold-Start Knowledge Base

The initial content is inherited from ARIS-in-AI-Offer:

![Cold-start knowledge base](Figure/Fig1.png)

The project organizes common AI interview and learning topics by subject, including principles, formula derivations, and code.

The main areas are:

- General AI foundations
- Post-training and alignment
- Model architectures
- Generative theory
- Generation systems
- Multimodal AI
- Agents

---

## Page Layout

The Yuan Knowledge Base frontend uses a three-column layout:

![Three-column Yuan Knowledge Base layout](Figure/Fig2.png)

### 1. Left: Knowledge Directory

Existing articles and new content generated through research are collected here for easy management and discovery.

### 2. Center: Reading Area

Knowledge chapters are stored in both `html` and `md` formats:

- `html` is for human reading.
- `md` is for agents to understand and process.

### 3. Right: Agent Workspace

You can chat directly with the agent or switch between different modes:

- Search
- Baguwen
- Interview
- Yuan Skill

Each mode combines one or more skills to support different learning and review tasks.

---

## Task Progress and Settings

The right-hand panel also includes dedicated Progress and Settings pages.

![Task progress and settings pages](Figure/Fig3.png)

For longer-running tasks, you can follow the current execution status on the Progress page.

The Settings page lets you switch between different agent backends.

## Main Features

## 1. Search: Add New Material to Your Knowledge Base

![Search and personalized knowledge generation](Figure/Fig4.png)

The workspace includes several research templates for web search, source organization, and knowledge generation.

In addition to regular research, you can upload a PDF resume to generate knowledge chapters tailored to your background.

---

## 2. Baguwen: Multi-Round Questions Based on a Knowledge Chapter

![Baguwen mode](Figure/Fig5.png)

Select **Baguwen**, and the agent will create an assessment sequence from the content on the current page and ask questions one round at a time.

Any gaps exposed during questioning can be organized into new knowledge entries for future review.

## 3. Interview: Mock Interviews Based on Your Resume and Job Requirements

![Mock interviews and scoring rules](Figure/Fig6.png)

Upload your resume, paste the target job description, and start a mock interview.

You can choose from several interviewer types, each with a different focus. The scoring rules also adapt to the role type.

## 4. Yuan Skill: Modify an Existing Skill or Create a New One

![Yuan Skill mode](Figure/Fig7.png)

Yuan Skill can modify an existing skill or create a new one.

Through several rounds of questions—inheriting its clarification workflow from maxkura/Ask_Why—it helps clarify your needs so the workspace can evolve around the way you work.
