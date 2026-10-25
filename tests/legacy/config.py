"""Factory for the historical classifier regression fixtures."""
from src.common.scrapper.config import ScrapperConfig as Config
from .strategy import JevStrategy

def ScrapperConfig(**kwargs):
    classifier=kwargs.pop('html_to_entities',None)
    options={k:kwargs.pop(k) for k in ('min_page_confidence','min_entity_confidence') if k in kwargs}
    if classifier is not None:
        if kwargs.get('strategy') is not None:
            raise ValueError('Duplicate strategy')
        kwargs['strategy']=JevStrategy(classifier=classifier,follow_navigation=kwargs.get('follow_navigation',True),**options)
    elif options:
        for name,value in options.items():
            if not 0<=value<=1:raise ValueError(name)
    return Config(**kwargs)
