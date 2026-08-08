import json
import os
import requests
import streamlit as st

st.set_page_config(
    page_title="Nepal & Global News Engine",
    page_icon="🇳🇵",
    layout="wide",
)

API_URL = "http://127.0.0.1:8000"

st.title("🇳🇵 Nepal & Global News Intelligence Engine")
st.caption("Factual News Retrieval and Synthesis for Nepalese Readers")

st.sidebar.header("Filter Settings")
region_filter = st.sidebar.selectbox("Target Region:", ["All", "Nepal", "International"])

# Category taxonomy isn't fixed like region — pull whatever actually exists
# in the DB rather than hardcoding a guessed list.
try:
    category_options = ["All"] + requests.get(f"{API_URL}/categories", timeout=5).json()
except Exception:
    category_options = ["All"]
category_filter = st.sidebar.selectbox("Category:", category_options)

top_k = st.sidebar.slider("Articles to retrieve (top_k):", min_value=1, max_value=15, value=5)
distance_threshold = st.sidebar.slider("Max Cosine Distance Cutoff:", min_value=0.1, max_value=1.0, value=0.55, step=0.05)
enable_llm = st.sidebar.checkbox("Enable LLM Synthesis Answer", value=True)

api_status_container = st.sidebar.empty()

try:
    health_check = requests.get(f"{API_URL}/", timeout=3)
    if health_check.status_code == 200:
        api_status_container.success("🟢 API Server Connected")
    else:
        api_status_container.error("🔴 API Server Error")
except Exception:
    api_status_container.error("❌ API Offline (`uvicorn main:app --reload`)")


def render_sources(sources):
    """Shared renderer for the article list, used by both the streaming
    LLM path and the plain /query path."""
    st.markdown("### 📄 Cited Context")
    st.divider()
    if not sources:
        st.warning("No articles matched the strict region and distance criteria.")
        return
    for idx, article in enumerate(sources, start=1):
        with st.container():
            col1, col2 = st.columns([4, 1])
            with col1:
                st.markdown(f"### {idx}. [{article['title']}]({article['url']})")
                region_label = (article['region'] or 'unknown').upper()
                category_label = (article['category'] or 'uncategorised').title()
                st.caption(
                    f"**Source:** {article['source']} | **Region:** {region_label} | **Category:** {category_label}"
                )
            with col2:
                st.metric(label="Cosine Distance", value=f"{article['distance']:.4f}")
            st.write(article["content"])
            st.divider()


def stream_synthesis(payload):
    """Posts to /synthesize/stream and yields (event_type, data) pairs as
    they arrive over Server-Sent Events. Raises on connection errors so the
    caller's existing try/except handles them the same way as before."""
    with requests.post(
        f"{API_URL}/synthesize/stream", json=payload, stream=True, timeout=60
    ) as resp:
        if resp.status_code != 200:
            yield "http_error", {"status_code": resp.status_code, "text": resp.text}
            return

        event_type = None
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            if raw_line.startswith("event:"):
                event_type = raw_line[len("event:"):].strip()
            elif raw_line.startswith("data:"):
                data = json.loads(raw_line[len("data:"):].strip())
                yield event_type, data


tab_search, tab_browse, tab_published = st.tabs(
    ["🔍 News Search & Synthesis", "📚 Ingested Database", "📰 Published Posts"]
)

with tab_search:
    st.subheader("Query News Database")

    with st.form(key="search_form"):
        query = st.text_input(
            "Enter topic or question:",
            placeholder="e.g., What are the latest developments in Nepal politics or AI regulations?",
        )
        submit_button = st.form_submit_button(label="Search Engine", type="primary")

    if submit_button or query:
        if not query.strip():
            st.warning("Please enter a valid search query.")
        else:
            selected_region = None if region_filter == "All" else region_filter.lower()
            selected_category = None if category_filter == "All" else category_filter.lower()

            if enable_llm:
                payload = {
                    "query": query,
                    "top_k": top_k,
                    "region": selected_region,
                    "category": selected_category,
                    "max_distance_threshold": distance_threshold,
                }

                st.markdown("### 🤖 Grounded News Summary")
                answer_box = st.empty()
                model_caption = st.empty()

                accumulated_text = ""
                sources = []
                error_detail = None

                try:
                    for event_type, data in stream_synthesis(payload):
                        if event_type == "http_error":
                            st.error(f"API Error ({data['status_code']}): {data['text']}")
                            break
                        elif event_type == "sources":
                            sources = data.get("sources_used", [])
                        elif event_type == "token":
                            accumulated_text += data.get("text", "")
                            # Cursor-style trailing marker while streaming in.
                            answer_box.info(accumulated_text + " ▌")
                        elif event_type == "done":
                            answer_box.info(accumulated_text)
                            model_name = data.get("model_used")
                            if model_name:
                                model_caption.caption(f"Answered by `{model_name}`")
                        elif event_type == "error":
                            error_detail = data.get("detail")

                    if error_detail:
                        if accumulated_text:
                            # Partial answer arrived before the failure — show
                            # what we got instead of throwing it away.
                            answer_box.info(accumulated_text)
                        st.error(f"Synthesis error: {error_detail}")

                except requests.exceptions.ConnectionError:
                    st.error("❌ Could not connect to FastAPI server. Ensure Uvicorn is running.")
                except Exception as e:
                    st.error(f"Unexpected error: {e}")

                st.divider()
                render_sources(sources)

            else:
                payload = {
                    "query": query,
                    "top_k": top_k,
                    "region": selected_region,
                    "category": selected_category,
                }
                with st.spinner("Searching..."):
                    try:
                        response = requests.post(f"{API_URL}/query", json=payload, timeout=20)
                        if response.status_code == 200:
                            data = response.json()
                            st.markdown(f"#### Found **{data.get('results_count')}** matching articles")
                            st.divider()
                            render_sources(data.get("articles", []))
                        else:
                            st.error(f"API Error ({response.status_code}): {response.text}")
                    except requests.exceptions.ConnectionError:
                        st.error("❌ Could not connect to FastAPI server. Ensure Uvicorn is running.")
                    except Exception as e:
                        st.error(f"Unexpected error: {e}")

with tab_browse:
    st.subheader("All Database Records")
    if st.button("Fetch Database Entries"):
        with st.spinner("Loading records..."):
            try:
                res = requests.get(f"{API_URL}/articles?limit=50", timeout=10)
                if res.status_code == 200:
                    records = res.json()
                    st.dataframe(records, use_container_width=True)
                else:
                    st.error(f"Error: {res.text}")
            except Exception as e:
                st.error(f"Error: {e}")

with tab_published:
    st.subheader("Published Posts")
    col_r, col_c, col_l = st.columns(3)
    pub_region = col_r.selectbox("Region", ["All", "Nepal", "International"], key="pub_region")
    pub_category = col_c.selectbox("Category", ["All"] + [
        o for o in category_options if o != "All"
    ], key="pub_cat")
    pub_limit = col_l.number_input("Show", min_value=5, max_value=50, value=10, step=5, key="pub_limit")

    if st.button("Load Published Posts", type="primary"):
        params = {"status": "published", "limit": int(pub_limit)}
        if pub_region != "All":
            params["region"] = pub_region.lower()
        if pub_category != "All":
            params["category"] = pub_category.lower()

        try:
            resp = requests.get(f"{API_URL}/posts", params=params, timeout=10)
            if resp.status_code == 200:
                posts = resp.json()
                if not posts:
                    st.info("No published posts yet.")
                else:
                    st.success(f"{len(posts)} post(s) found.")
                    for post in posts:
                        lang_flag = "🇳🇵" if post.get("language") == "nepali" else "🌐"
                        region_label = (post.get("region") or "unknown").title()
                        category_label = (post.get("category") or "general").title()
                        with st.expander(
                            f"{lang_flag} [{region_label} / {category_label}] Post #{post['id']}  —  {post.get('created_at', '')[:10]}"
                        ):
                            if post.get("image_url"):
                                st.image(post["image_url"], use_container_width=True)
                                if post.get("image_source_credit"):
                                    st.caption(post["image_source_credit"])
                            st.markdown("**Social Summary**")
                            st.info(post.get("social_summary", ""))
                            st.markdown("**Full Article**")
                            st.write(post.get("full_body", ""))
            else:
                st.error(f"API error ({resp.status_code}): {resp.text}")
        except requests.exceptions.ConnectionError:
            st.error("❌ Could not connect to FastAPI server.")