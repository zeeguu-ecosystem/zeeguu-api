"""

    Goes through all the interesting sources that the server knows
    about and downloads new articles saving them in the DB.  


"""

import newspaper

from pymysql import DataError

from zeeguu.core.content_retriever.crawler_exceptions import *
from zeeguu.logging import log, logp

from zeeguu.core import model
from zeeguu.core.semantic_search import semantic_search_add_topics_based_on_neigh
from zeeguu.core.content_quality.quality_filter import sufficient_quality
from zeeguu.core.emailer.zeeguu_mailer import ZeeguuMailer
from zeeguu.core.model import Url, Feed, LocalizedTopic, TopicKeyword, NewTopic
from zeeguu.core.model.new_article_topic_map import TopicOriginType
import requests

from zeeguu.core.model.article import MAX_CHAR_COUNT_IN_SUMMARY

from sentry_sdk import capture_exception as capture_to_sentry
from zeeguu.core.elastic.indexing import index_in_elasticsearch

from zeeguu.core.content_retriever import download_and_parse

TIMEOUT_SECONDS = 10


import zeeguu

LOG_CONTEXT = "FEED RETRIEVAL"


def _url_after_redirects(url):
    # solve redirects and save the clean url
    response = requests.get(url)
    return response.url


def _date_in_the_future(time):
    from datetime import datetime

    return time > datetime.now()


def banned_url(url):
    banned = [
        "https://www.dr.dk/sporten/seneste-sport/",
        "https://www.dr.dk/nyheder/seneste/",
    ]
    for each in banned:
        if url.startswith(each):
            return True
    return False


def download_from_feed(feed: Feed, session, limit=1000, save_in_elastic=True):
    """

    Session is needed because this saves stuff to the DB.


    last_crawled_time is useful because otherwise there would be a lot of time
    wasted trying to retrieve the same articles, especially the ones which
    can't be retrieved, so they won't be cached.


    """

    print(feed.url)

    downloaded = 0
    skipped_due_to_low_quality = 0
    skipped_already_in_db = 0

    last_retrieval_time_from_DB = None
    last_retrieval_time_seen_this_crawl = None

    if feed.last_crawled_time:
        last_retrieval_time_from_DB = feed.last_crawled_time
        log(f"LAST CRAWLED::: {last_retrieval_time_from_DB}")

    try:
        items = feed.feed_items(last_retrieval_time_from_DB)
    except Exception as e:
        import traceback

        traceback.print_stack()
        capture_to_sentry(e)
        return

    for feed_item in items:

        skipped_already_in_db = 0

        if downloaded >= limit:
            break

        feed_item_timestamp = feed_item["published_datetime"]

        if _date_in_the_future(feed_item_timestamp):
            log("Article from the future!")
            continue

        if (not last_retrieval_time_seen_this_crawl) or (
            feed_item_timestamp > last_retrieval_time_seen_this_crawl
        ):
            last_retrieval_time_seen_this_crawl = feed_item_timestamp

        if last_retrieval_time_seen_this_crawl > feed.last_crawled_time:
            feed.last_crawled_time = last_retrieval_time_seen_this_crawl
            log(
                f"+updated feed's last crawled time to {last_retrieval_time_seen_this_crawl}"
            )

        try:
            log("before redirects")
            log(feed_item["url"])
            url = _url_after_redirects(feed_item["url"])
            logp("===============================> ")
            logp("after redirects")
            logp(url)

        except requests.exceptions.TooManyRedirects:
            raise Exception(f"- Too many redirects")
        except Exception:
            raise Exception(
                f"- Could not get url after redirects for {feed_item['url']}"
            )

        if banned_url(url):
            logp("Banned Url")
            continue

        session.add(feed)
        session.commit()

        try:
            new_article = download_feed_item(session, feed, feed_item, url)
            downloaded += 1
            if save_in_elastic:
                if new_article:
                    index_in_elasticsearch(new_article, session)

            if new_article:
                ZeeguuMailer.send_content_retrieved_notification(new_article)

        except SkippedForTooOld:
            logp("- Article too old")
            continue

        except SkippedForLowQuality as e:
            logp(f" - Low quality: {e.reason}")
            skipped_due_to_low_quality += 1
            continue

        except SkippedAlreadyInDB:
            skipped_already_in_db += 1
            logp(" - Already in DB")
            continue

        except FailedToParseWithReadabilityServer as e:
            logp(f" - failed to parse with readability server (server said: {e})")
            continue

        except newspaper.ArticleException as e:
            logp(f"Newspaper can't download article at: {url}")
            continue

        except DataError as e:
            logp(f"Data error ({e}) for: {url}")
            continue

        except requests.exceptions.Timeout:
            logp(
                f"The request from the server was timed out after {TIMEOUT_SECONDS} seconds."
            )
            continue

        except Exception as e:
            import traceback

            traceback.print_stack()
            capture_to_sentry(e)
            if hasattr(e, "message"):
                logp(e.message)
            else:
                logp(e)
            continue

    logp(f"*** Downloaded: {downloaded} From: {feed.title}")
    logp(f"*** Low Quality: {skipped_due_to_low_quality}")
    logp(f"*** Already in DB: {skipped_already_in_db}")
    logp(f"*** ")


def download_feed_item(session, feed, feed_item, url):
    title = feed_item["title"]

    published_datetime = feed_item["published_datetime"]

    art = model.Article.find(url)

    if art:
        raise SkippedAlreadyInDB()

    np_article = download_and_parse(url)

    is_quality_article, reason = sufficient_quality(np_article)

    if not is_quality_article:
        raise SkippedForLowQuality(reason)

    summary = feed_item["summary"]
    # however, this is not so easy... there have been cases where
    # the summary is just malformed HTML... thus we try to extract
    # the text:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(summary, "lxml")
    summary = soup.get_text()
    # then there are cases where the summary is huge... so we clip it
    summary = summary[:MAX_CHAR_COUNT_IN_SUMMARY]
    # and if there is still no summary, we simply use the beginning of
    # the article
    if len(summary) < 10:
        summary = np_article.text[:MAX_CHAR_COUNT_IN_SUMMARY]

    # Create new article and save it to DB
    new_article = zeeguu.core.model.Article(
        Url.find_or_create(session, url),
        title,
        ", ".join(np_article.authors),
        np_article.text,
        summary,
        published_datetime,
        feed,
        feed.language,
        htmlContent=np_article.htmlContent,
    )

    if np_article.top_image != "":
        new_article.img_url = Url.find_or_create(session, np_article.top_image)
    session.add(new_article)

    topics = add_topics(new_article, session)
    logp(f" Topics ({topics})")
    topic_keywords = add_topic_keywords(new_article, session)
    logp(f" Topic Keywords: ({topic_keywords})")
    # compute extra difficulties for french articles
    try:
        if new_article.language.code == "fr":
            from zeeguu.core.language.services.lingo_rank_service import (
                retrieve_lingo_rank,
            )

            df = DifficultyLingoRank(
                new_article, retrieve_lingo_rank(new_article.content)
            )
            session.add(df)
        parsed = download_and_parse(url)

        is_quality_article, reason = sufficient_quality(parsed)

        if not is_quality_article:
            raise SkippedForLowQuality(reason)

        summary = feed_item["summary"]
        # however, this is not so easy... there have been cases where
        # the summary is just malformed HTML... thus we try to extract
        # the text:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(summary, "lxml")
        summary = soup.get_text()
        # then there are cases where the summary is huge... so we clip it
        summary = summary[:MAX_CHAR_COUNT_IN_SUMMARY]
        # and if there is still no summary, we simply use the beginning of
        # the article
        if len(summary) < 10:
            summary = parsed.text[:MAX_CHAR_COUNT_IN_SUMMARY]

            # Create new article and save it to DB
        new_article = zeeguu.core.model.Article(
            Url.find_or_create(session, url),
            title,
            ", ".join(parsed.authors),
            parsed.text,
            summary,
            published_datetime,
            feed,
            feed.language,
        )
        session.add(new_article)

        # Update topics based on the keywords:
        old_topics = add_topics(new_article, session)
        logp(f"Old Topics ({old_topics})")
        topic_keywords = add_topic_keywords(new_article, session)
        logp(f"Topic Keywords: ({topic_keywords})")
        origin_type, topics = add_new_topics(new_article, feed, topic_keywords, session)
        logp(f"New Topics ({topics})")
    except SkippedForLowQuality as e:
        raise e

    except newspaper.ArticleException as e:
        logp(f"can't download article at: {url}")

    except DataError as e:
        logp(f"Data error for: {url}")

    except requests.exceptions.Timeout:
        logp(
            f"The request from the server was timed out after {TIMEOUT_SECONDS} seconds."
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        capture_to_sentry(e)

        log(
            f"* Rolling back session due to exception while creating article and attaching words/topics: {str(e)}"
        )
        session.rollback()
    session.commit()
    logp(f"SUCCESS for: {new_article.title}")

    return new_article


def add_topics(new_article, session):
    topics = []
    for loc_topic in LocalizedTopic.query.all():
        if loc_topic.language == new_article.language and loc_topic.matches_article(
            new_article
        ):
            topics.append(loc_topic.topic.title)
            new_article.add_topic(loc_topic.topic)
            session.add(new_article)
    return topics


def add_new_topics(new_article, feed, topic_keywords, session):
    HARDCODED_FEEDS = {102: 8}
    # Handle Hard coded Feeds
    if feed.id in HARDCODED_FEEDS:
        print("Used HARDCODED feed")
        topic = NewTopic.find_by_id(HARDCODED_FEEDS[feed.id])
        new_article.add_new_topic(topic, session, TopicOriginType.HARDSET.value)
        session.add(new_article)
        return TopicOriginType.HARDSET.value, [topic.title]
    # Try setting the Topics based on URLs
    topics = []
    topics_added = set()
    for topic_key in topic_keywords:
        topic = topic_key.topic
        print(topic_key, topic)
        if topic is not None:
            if topic.id in topics_added:
                continue
            topics_added.add(topic.id)
            topics.append(topic)
            new_article.add_new_topic(topic, session, TopicOriginType.URL_PARSED.value)
            session.add(new_article)
    if len(topics) > 0:
        print("Used URL PARSED")
        return TopicOriginType.URL_PARSED.value, [t.title for t in topics]

    from collections import Counter

    # Add based on KK neighbours:
    a_found_t, _ = semantic_search_add_topics_based_on_neigh(new_article)
    neighbouring_topics = [t.new_topic for a in a_found_t for t in a.new_topics]
    if len(neighbouring_topics) > 0:
        topics_counter = Counter(neighbouring_topics)
        top_topic, count = topics_counter.most_common(1)[0]
        print(topics_counter)
        if count >= 1:
            print("Used INFERRED")
            new_article.add_new_topic(
                top_topic, session, TopicOriginType.INFERRED.value
            )
            session.add(new_article)
            return TopicOriginType.INFERRED.value, [top_topic.title]
    return None, []


def add_topic_keywords(new_article, session):
    topic_keywords = [
        TopicKeyword.find_or_create(session, keyword, new_article.language)
        for keyword in TopicKeyword.get_topic_keywords_from_url(new_article.url)
        if keyword is not None
    ]
    new_article.set_topic_keywords(topic_keywords, session)
    session.add(new_article)
    return topic_keywords
