import pytest
from fastapi import Form
from pydantic import BaseModel
from utils.helpers import as_form

def test_as_form_decorator():
    @as_form
    class TestForm(BaseModel):
        name: str
        age: int = 18
        
    assert hasattr(TestForm, 'as_form')
    
    # Check signature
    import inspect
    sig = inspect.signature(TestForm.as_form)
    assert 'name' in sig.parameters
    assert 'age' in sig.parameters
    
    # Check defaults
    assert isinstance(sig.parameters['name'].default, type(Form(...)))
    assert isinstance(sig.parameters['age'].default, type(Form(18)))
